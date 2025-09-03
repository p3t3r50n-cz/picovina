#!/bin/bash

# Non-locale numbers for consistent decimal formatting
export LC_ALL=C

# Configuration
BAT_CELLS=3
BAT_CELL_CAPACITY=2600
BAT_VOLTAGE_HIGH=4128
BAT_VOLTAGE_LOW=3100
VOLTAGE_HYSTERESIS=50  # mV hysteresis for full charge detection
BAT_FULL_CLAMP=100

I2C_BUS=2
I2C_ADDR=0x41

# Pre-calculated constants
BAT_CAPACITY=$((BAT_CELLS * BAT_CELL_CAPACITY))
BAT_VOLTAGE_FULL=$((BAT_VOLTAGE_HIGH * BAT_CELLS))
BAT_VOLTAGE_EMPTY=$((BAT_VOLTAGE_LOW * BAT_CELLS))
BAT_VOLTAGE_HYSTERESIS=$((VOLTAGE_HYSTERESIS * BAT_CELLS))

# INA219 Calibration
CALIBRATION=26868
POWER_LSB=0.003048
CURRENT_LSB=0.1524
SHUNT_RESISTOR=0.01

# Files
BATFILE="/dev/pi_battery"
CURRENT_HISTORY_FILE="/dev/shm/current.history"
VOLTAGE_HISTORY_FILE="/dev/shm/voltage.history"
SHUNT_HISTORY_FILE="/dev/shm/shunt.history"
POWER_HISTORY_FILE="/dev/shm/power.history"
CALIBRATION_FILE="/var/lib/batmon/calibration_data"

# Registry addresses
REG_CONFIG=0x00
REG_SHUNTVOLTAGE=0x01
REG_BUSVOLTAGE=0x02
REG_POWER=0x03
REG_CURRENT=0x04
REG_CALIBRATION=0x05

# Initialize variables
DYNAMIC_CHARGE_FULL=$((BAT_CAPACITY * 1000))  # in μAh
LAST_CALIBRATION_TIME=0
CALIBRATION_INTERVAL=3600  # calibrate at most once per hour

# Ensure calibration directory exists
mkdir -p "$(dirname "$CALIBRATION_FILE")"

# Load previous calibration data if exists
if [ -f "$CALIBRATION_FILE" ]; then
	source "$CALIBRATION_FILE"
fi

# Initialize history files
touch "$CURRENT_HISTORY_FILE" "$VOLTAGE_HISTORY_FILE" "$SHUNT_HISTORY_FILE" "$POWER_HISTORY_FILE"

function write_register() {
	local reg=$1 value=$2
	i2cset -y $I2C_BUS $I2C_ADDR $reg \
		$(( (value >> 8) & 0xFF )) $(( value & 0xFF )) i
}

function read_register() {
	local reg=$1
	local data
	data=$(i2cget -y $I2C_BUS $I2C_ADDR $reg w)
	echo $(( ( (data & 0xFF) << 8 ) | ( (data >> 8) & 0xFF ) ))
}

function average() {
	local value=$1
	local file=$2
	local window=20 maxcount=500
	
	# Append new value and maintain history
	echo "$value" >> "$file"
	tail -n "$maxcount" "$file" > "${file}.tmp"
	mv "${file}.tmp" "$file"
	
	# Calculate average
	awk -v win="$window" '
		{sum+=$1; count++}
		END {
			if(count >= win) printf "%.3f", sum/count
			else printf "%.3f", '$value'
		}' "$file"
}

# Dynamic battery capacity calibration
function calibrate_battery() {
	local current_time=$(date +%s)
	local voltage=$1
	local charge_now=$2

	# Calibrate only if enough time passed since last calibration
	if (( current_time - LAST_CALIBRATION_TIME < CALIBRATION_INTERVAL )); then
		return
	fi

	# Calibrate only if battery is fully charged (voltage above hysteresis threshold)
	if (( voltage >= BAT_VOLTAGE_FULL - BAT_VOLTAGE_HYSTERESIS )); then
		# If current capacity is lower than dynamic full capacity, adjust it
		if (( charge_now < DYNAMIC_CHARGE_FULL )); then
			DYNAMIC_CHARGE_FULL=$(( (DYNAMIC_CHARGE_FULL * 19 + charge_now) / 20 ))  # smooth change
			LAST_CALIBRATION_TIME=$current_time
			
			# Save calibration data
			echo ">>> Saving battery calibration data..."
			{
				echo "DYNAMIC_CHARGE_FULL=$DYNAMIC_CHARGE_FULL"
				echo "LAST_CALIBRATION_TIME=$LAST_CALIBRATION_TIME"
			} > "$CALIBRATION_FILE"
		fi
	fi
}

# INA219 calibration setup
function setCalibration() {
	# Write calibration value
	write_register $REG_CALIBRATION $CALIBRATION
	
	# Configure mode (16V, 320mV, 12bit x32 samples, continuous mode)
	local CONFIG=$(( (0x00 << 13) | (0x03 << 11) | (0x0D << 7) | (0x0D << 3) | 0x07 ))
	write_register $REG_CONFIG $CONFIG
}

# Initialize INA219
setCalibration
sleep 1

voltage_min_design=$((BAT_VOLTAGE_EMPTY))

# Main monitoring loop
while true; do
	
	# Read all measurements
	bus_raw=$(read_register $REG_BUSVOLTAGE)
	shunt_raw=$(read_register $REG_SHUNTVOLTAGE)
	current_raw=$(read_register $REG_CURRENT)
	power_raw=$(read_register $REG_POWER)
	
	# Process measurements
	
	# Bus voltage
	bus_voltage=$(( (bus_raw >> 3) * 4 ))
	bus_voltage_avg=$(printf "%.0f" $(average "$bus_voltage" "$VOLTAGE_HISTORY_FILE"))
	
	# Shunt voltage
	shunt_voltage=$(awk -v raw="$shunt_raw" 'BEGIN {
		if (raw > 32767) printf "%.3f", ((raw - 65535) * 0.01)
		else printf "%.3f", (raw * 0.01)
	}')
	shunt_voltage_abs=${shunt_voltage#-}
	shunt_voltage_avg=$(average "$shunt_voltage_abs" "$SHUNT_HISTORY_FILE")
	
	# Current
	current=$(awk -v raw="$current_raw" -v lsb="$CURRENT_LSB" 'BEGIN {
		if (raw > 32767) printf "%.3f", (((raw - 65535) * lsb) / 1000)
		else printf "%.3f", ((raw * lsb) / 1000)
	}')
	current_abs=${current#-}
	current_avg=$(average "$current_abs" "$CURRENT_HISTORY_FILE")
	
	# Power
	power=$(awk -v raw="$power_raw" -v lsb="$POWER_LSB" 'BEGIN {
		if (raw > 32767) printf "%.3f", ((raw - 65535) * lsb)
		else printf "%.3f", (raw * lsb)
	}')
	power_avg=$(average "$power" "$POWER_HISTORY_FILE")
	
	# Battery percentage calculation
	battery_percent=$(awk \
		-v v="$bus_voltage_avg" \
		-v full="$BAT_VOLTAGE_FULL" \
		-v empty="$BAT_VOLTAGE_EMPTY" '
		BEGIN {
			if (v >= full) print 100
			else if (v <= empty) print 0
			else {
				result = (v - empty) * 100 / (full - empty)
				print (result == int(result)) ? result : int(result) + 1
			}
		}')
	
	# Convert to μAh
	charge_full=$DYNAMIC_CHARGE_FULL
	charge_now=$(( (charge_full * battery_percent) / 100 ))
	current_now=$(awk -v avg="$current_avg" 'BEGIN {print int(avg * 1000000)}')
	
	# Calibrate battery if almost fully charged
	calibrate_battery "$bus_voltage" "$charge_now"
	
	# Battery status detection with shunt voltage hysteresis
	threshold1=-3.0
	threshold2=0.5
	battery_status_int=$(awk -v x="$shunt_voltage" -v t1="$threshold1" -v t2="$threshold2" 'BEGIN {
		if (x < t1) print 2        # discharging
		else if (x > t2) print 1    # charging  
		else print 0               # full or small current
	}')
	
	# Clamping at BAT_FULL_CLAMP
	if [ "$battery_percent" -ge $BAT_FULL_CLAMP ] && { [ "$battery_status_int" -eq 0 ] || [ "$battery_status_int" -eq 1 ]; }; then
		battery_percent=100
		charge_now=$charge_full
		current_now=1000
		battery_status_int=0
	fi
	
	# Calculate remaining time
	if [ "$battery_status_int" -eq 2 ]; then
		# Discharging: time to empty = (charge_now / current_now)
		battery_remain_sec=$(awk -v now="$charge_now" -v cur="$current_now" 'BEGIN{
			if (cur <= 0) print 0; else print int((now / cur) * 3600) }')
		battery_remain_time=$(awk -v sec="$battery_remain_sec" 'BEGIN{
			hours = int(sec/3600); minutes = int((sec%3600)/60);
			printf "%d h %02d min", hours, minutes }')
	elif [ "$battery_status_int" -eq 1 ]; then
		# Charging: time to full = (charge_full - charge_now) / current_now
		battery_charge_sec=$(awk -v full="$charge_full" -v now="$charge_now" -v cur="$current_now" 'BEGIN{
			if (cur <= 0) print 0; else print int(((full - now) / cur) * 3600) }')
		battery_remain_time=$(awk -v sec="$battery_charge_sec" 'BEGIN{
			hours = int(sec/3600); minutes = int((sec%3600)/60);
			printf "%d h %02d min", hours, minutes }')
	else
		battery_remain_time="Fully charged"
	fi
	
	case $battery_status_int in
		0) battery_status="Full" ;;
		1) battery_status="Charging" ;;
		2) battery_status="Discharging" ;;
		*) battery_status="n/a" ;;
	esac
	
	(
		
		echo "--- ["`date '+%Y-%m-%d %H:%M:%S'`"] -------------------------"
		echo
		
		# Display measurements
		echo "Battery values"
		echo "---------------------------------------------------"
		echo "bus_raw:             $bus_raw"
		echo "bus_voltage:         $bus_voltage mV"
		echo "bus_voltage_avg:     $bus_voltage_avg mV"
		echo
		echo "shunt_raw:           $shunt_raw"
		echo "shunt_voltage:       $shunt_voltage mV"
		echo "shunt_voltage_avg:   $shunt_voltage_avg mV"
		echo
		echo "current_raw:         $current_raw"
		echo "current:             $current A"
		echo "current_avg:         $current_avg A"
		echo
		echo "power:               $power W"
		echo "power_avg:           $power_avg W"
		echo
		
		# Display battery info
		echo "Battery info"
		echo "---------------------------------------------------"
		echo "Design capacity:     $BAT_CAPACITY mAh ($BAT_CELL_CAPACITY mAh * $BAT_CELLS)"
		echo "Last max. capacity:  $((DYNAMIC_CHARGE_FULL / 1000)) mAh"
		echo "Remaining capacity:  $((charge_now / 1000)) mAh"
		echo
		echo "Voltage:             $bus_voltage mV (min. design: $voltage_min_design mV)"
		echo "Current:             $current_avg A"
		echo "Power:               $power W"
		echo
		echo "Status:              $battery_status"
		echo "Charge:              $battery_percent %"
		echo "Remaining time:      $battery_remain_time"
		echo
		
		# Write to battery file
		echo "Data written to $BATFILE"
		echo "---------------------------------------------------"
		{
			echo "voltage_min_design=$(( voltage_min_design * 1000 ))"
			echo "voltage_now=$(( bus_voltage * 1000 ))"
			echo "current_now=$current_now"
			echo "charge_full_design=$(( BAT_CAPACITY * 1000 ))"
			echo "charge_full=$charge_full"
			echo "charge_now=$charge_now"
			echo "capacity=$battery_percent"
			
			if [ "$battery_status_int" -eq 1 ] || [ "$battery_status_int" -eq 0 ]; then
				echo "charging=1"
			else
				echo "charging=0"
			fi
		} >$BATFILE #| tee "$BATFILE"
		echo
	) | tee /dev/shm/pi_battery.log
	
	sleep 2
done
