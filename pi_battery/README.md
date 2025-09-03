# Overview
This is a modified version of [rpi-integrated-battery-module](https://github.com/a8ksh4/rpi-integrated-battery-module), originally based on [linux-fake-battery-module](https://github.com/hoelzro/linux-fake-battery-module).

It creates a virtual Linux battery that integrates with Li-Ion monitoring chips like the INA219 (Waveshare UPS Module 3S in my case). Unlike the original project, this version is simplified to support a **single battery** instead of two, and includes several usability improvements.

A background service queries the INA219 chip over I²C and updates the kernel module with battery percentage and charging status, making it appear like a standard laptop battery on the system taskbar.

## Tested Configuration
- **Board**: Orange Pi 5 MAX  
- **UPS Module**: Waveshare UPS Module 3S  
- **OS**: Armbian (25.11.0-trunk.118 bookworm) with vendor kernel (6.1.115-vendor-rk35xx)

# Features
- Single battery support (simplified from original dual-battery design)
- Linux kernel module integration  
- I²C service to read INA219 battery data
- Lightweight and easily adaptable to other SBCs

# Notes
- Based on the original work of the rpi-integrated-battery-module project
- Intended for custom SBC setups (e.g., Orange Pi, Raspberry Pi)
- Licensed under GPLv3

# Features

- Single battery support (simplified)  
- Linux kernel module integration  
- I²C service to read INA219 battery data  
- Lightweight and easy to adapt to other SBCs  

# Notes

- Based on the original work of the rpi-integrated-battery-module project  
- Intended for custom SBC setups (e.g., Orange Pi, Raspberry Pi)  
- Licensed under GPLv3  

# Installation

## Kernel module

```bash
cd pi_battery_module  
make install  
cp pi-battery.ko /usr/lib/modules/
insmod /lib/modules/pi-battery.ko
```

After this, the device `/dev/pi_battery` should be created. You can write values to it like:

```bash
echo "current_now = 1234" >/dev/pi_battery  
echo "voltage_now = 1234" >/dev/pi_battery  
```

The battery module supports these keys:

- `voltage_min_design` – minimal design voltage  
- `voltage_now` – current measured voltage  
- `current_now` – current measured current  
- `charge_full_design` – full designed charge  
- `charge_full` – actual maximum charge  
- `charge_now` – current measured charge  
- `capacity` – current measured capacity  

## Systemd service

The service for reading data from the chip, calculating values, and updating events in the kernel module was originally created as a **BASH script**, which I am familiar with. Later, I had an **AI translate it into Python** (which I don’t know at all), and it seems to work just as well—and probably even more efficiently—than my original BASH script. The project includes **both versions**, so you can choose whichever works best for you. ;-)

### Using the BASH script

```bash
cp pi-battery.sh /usr/local/bin/pi-battery  
chmod +x /usr/local/bin/pi-battery  
```

### Using the Python script

```bash
cp pi-battery.py /usr/local/bin/pi-battery  
chmod +x /usr/local/bin/pi-battery  
```

### Setup systemd service

```bash
cp pi-battery.service /etc/systemd/system/  
systemctl daemon-reload  
systemctl enable pi-battery.service  
systemctl start pi-battery.service
```

----

You can run both scripts manually without using a systemd service, but by default, only the BASH script writes its output to STDOUT.

To see debug output with Python, set the environment variable `DEBUG=1`:

```bash
DEBUG=1 pi-battery.py  
```