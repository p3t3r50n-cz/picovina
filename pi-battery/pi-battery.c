/*
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

/* Based heavily on https://git.kernel.org/cgit/linux/kernel/git/stable/linux-stable.git/tree/drivers/power/test_power.c?id=refs/tags/v4.2.6 */

#include <linux/fs.h>
#include <linux/kernel.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/power_supply.h>
#include <linux/string.h>

#include <asm/uaccess.h>

static int
pi_battery_get_property(struct power_supply *psy,
        enum power_supply_property psp,
        union power_supply_propval *val);

static int
pi_ac_get_property(struct power_supply *psy,
        enum power_supply_property psp,
        union power_supply_propval *val);

static struct battery_status {
    int status;
    int voltage_min_design;
    int voltage_now;
    int current_now;
    int charge_full_design;
    int charge_full;
    int charge_now;
    int capacity;
    int capacity_level;
} pi_battery_status = {
    .status = POWER_SUPPLY_STATUS_FULL,
    .voltage_min_design = 0,
    .voltage_now = 0,
    .current_now = 0,
    .charge_full_design = 0,
    .charge_full = 0,
    .charge_now = 0,
    .capacity = 0,
    .capacity_level = POWER_SUPPLY_CAPACITY_LEVEL_FULL
};

static int ac_status = 1;

static char *pi_ac_supplies[] = {
    "BAT0",
};

static enum power_supply_property pi_battery_properties[] = {
    POWER_SUPPLY_PROP_STATUS,
    POWER_SUPPLY_PROP_VOLTAGE_MIN_DESIGN,
    POWER_SUPPLY_PROP_VOLTAGE_NOW,
    POWER_SUPPLY_PROP_CURRENT_NOW,
    POWER_SUPPLY_PROP_CHARGE_FULL_DESIGN,
    POWER_SUPPLY_PROP_CHARGE_FULL,
    POWER_SUPPLY_PROP_CHARGE_NOW,
    POWER_SUPPLY_PROP_CAPACITY,
    POWER_SUPPLY_PROP_CAPACITY_LEVEL,
    
    POWER_SUPPLY_PROP_CHARGE_TYPE,
    POWER_SUPPLY_PROP_HEALTH,
    POWER_SUPPLY_PROP_PRESENT,
    POWER_SUPPLY_PROP_TECHNOLOGY,

    POWER_SUPPLY_PROP_MODEL_NAME,
    POWER_SUPPLY_PROP_MANUFACTURER,
    POWER_SUPPLY_PROP_SERIAL_NUMBER,
    
};

static enum power_supply_property pi_ac_properties[] = {
    POWER_SUPPLY_PROP_ONLINE,
};

static struct power_supply_desc descriptions[] = {
    {
        .name = "BAT0",
        .type = POWER_SUPPLY_TYPE_BATTERY,
        .properties = pi_battery_properties,
        .num_properties = ARRAY_SIZE(pi_battery_properties),
        .get_property = pi_battery_get_property,
    },

    {
        .name = "AC0",
        .type = POWER_SUPPLY_TYPE_MAINS,
        .properties = pi_ac_properties,
        .num_properties = ARRAY_SIZE(pi_ac_properties),
        .get_property = pi_ac_get_property,
    },
};

static struct power_supply_config configs[] = {
    { },
    {
        .supplied_to = pi_ac_supplies,
        .num_supplicants = ARRAY_SIZE(pi_ac_supplies),
    },
};

static struct power_supply *supplies[ARRAY_SIZE(descriptions)];

static ssize_t
control_device_read(struct file *file, char *buffer, size_t count, loff_t *ppos)
{
    static const char *message = "Pi battery information!";
    size_t message_len = strlen(message);

    if (*ppos != 0)
        return 0;

    if (count < message_len)
        return -EINVAL;

    if (copy_to_user(buffer, message, message_len))
        return -EFAULT;

    *ppos = message_len;
    return message_len;
}

#define prefixed(s, prefix) \
    (!strncmp((s), (prefix), sizeof(prefix)-1))

static int
handle_control_line(const char *line, int *ac_status, struct battery_status *battery)
{
    char *value_p;
    long value;
    int status;

    value_p = strchrnul(line, '=');
    if (!*value_p)
        return -EINVAL;

    value_p = skip_spaces(value_p + 1);
    status = kstrtol(value_p, 10, &value);
    if (status)
        return status;

    if (prefixed(line, "voltage_min_design")) {
        battery->voltage_min_design = value;
    }
    else if (prefixed(line, "voltage_now")) {
        battery->voltage_now = value;
    }
    else if (prefixed(line, "current_now")) {
        battery->current_now = value;
    }
    else if (prefixed(line, "charge_full_design")) {
        battery->charge_full_design = value;
    }
    else if (prefixed(line, "charge_full")) {
        battery->charge_full = value;
    }
    else if (prefixed(line, "charge_now")) {
        battery->charge_now = value;
    }
    else if (prefixed(line, "capacity")) {
        battery->capacity = value;
    }
    else if(prefixed(line, "charging")) {
        *ac_status = value;
    }
    else {
        return -EINVAL;
    }

    return 0;
}

static void
handle_charge_changes(int ac_status, struct battery_status *battery)
{
    if (ac_status) {
        if (battery->capacity < 100)
            battery->status = POWER_SUPPLY_STATUS_CHARGING;
        else
            battery->status = POWER_SUPPLY_STATUS_FULL;
    } else {
        battery->status = POWER_SUPPLY_STATUS_DISCHARGING;
    }

    if (battery->capacity >= 98)
        battery->capacity_level = POWER_SUPPLY_CAPACITY_LEVEL_FULL;
    else if (battery->capacity >= 70)
        battery->capacity_level = POWER_SUPPLY_CAPACITY_LEVEL_HIGH;
    else if (battery->capacity >= 30)
        battery->capacity_level = POWER_SUPPLY_CAPACITY_LEVEL_NORMAL;
    else if (battery->capacity >= 5)
        battery->capacity_level = POWER_SUPPLY_CAPACITY_LEVEL_LOW;
    else
        battery->capacity_level = POWER_SUPPLY_CAPACITY_LEVEL_CRITICAL;

    //battery->time_left = 36 * battery->capacity;
}

static ssize_t
control_device_write(struct file *file, const char *buffer, size_t count, loff_t *ppos)
{
    char kbuffer[1024];
    char *buffer_cursor;
    char *newline;
    size_t bytes_left = count;
    int status;

    if (*ppos != 0) {
        printk(KERN_ERR "writes to /dev/pi_battery must be completed in a single system call\n");
        return -EINVAL;
    }

    if (count > sizeof(kbuffer)) {
        printk(KERN_ERR "Too much data provided to /dev/pi_battery (limit %lu bytes)\n", sizeof(kbuffer));
        return -EINVAL;
    }

    if (copy_from_user(kbuffer, buffer, count))
        return -EFAULT;

    buffer_cursor = kbuffer;
    while ((newline = memchr(buffer_cursor, '\n', bytes_left))) {
        *newline = '\0';
        status = handle_control_line(buffer_cursor, &ac_status, &pi_battery_status);
        if (status)
            return status;

        bytes_left -= (newline - buffer_cursor) + 1;
        buffer_cursor = newline + 1;
    }

    handle_charge_changes(ac_status, &pi_battery_status);

    power_supply_changed(supplies[0]);
    power_supply_changed(supplies[1]);

    return count;
}

static const struct file_operations control_device_ops = {
    .owner = THIS_MODULE,
    .read = control_device_read,
    .write = control_device_write,
};

static struct miscdevice control_device = {
    .minor = MISC_DYNAMIC_MINOR,
    .name = "pi_battery",
    .fops = &control_device_ops,
};

static int
pi_battery_get_property(struct power_supply *psy,
        enum power_supply_property psp,
        union power_supply_propval *val)
{

    switch (psp) {
        case POWER_SUPPLY_PROP_MODEL_NAME:
            val->strval = "Pi battery";
            break;
        case POWER_SUPPLY_PROP_SERIAL_NUMBER:
            val->strval = "P1B4TT3RY";
            break;
        case POWER_SUPPLY_PROP_MANUFACTURER:
            val->strval = "Pi";
            break;
        case POWER_SUPPLY_PROP_STATUS:
            val->intval = pi_battery_status.status;
            break;
        case POWER_SUPPLY_PROP_CHARGE_TYPE:
            val->intval = POWER_SUPPLY_CHARGE_TYPE_FAST;
            break;
        case POWER_SUPPLY_PROP_HEALTH:
            val->intval = POWER_SUPPLY_HEALTH_GOOD;
            break;
        case POWER_SUPPLY_PROP_PRESENT:
            val->intval = 1;
            break;
        case POWER_SUPPLY_PROP_TECHNOLOGY:
            val->intval = POWER_SUPPLY_TECHNOLOGY_LION;
            break;
        case POWER_SUPPLY_PROP_CAPACITY_LEVEL:
            val->intval = pi_battery_status.capacity_level;
            break;
        case POWER_SUPPLY_PROP_CAPACITY:
            val->intval = pi_battery_status.capacity;
            break;
        case POWER_SUPPLY_PROP_CHARGE_NOW:
            val->intval = pi_battery_status.charge_now;
            break;
        case POWER_SUPPLY_PROP_CHARGE_FULL_DESIGN:
            val->intval = pi_battery_status.charge_full_design;
            break;
        case POWER_SUPPLY_PROP_CHARGE_FULL:
            val->intval = pi_battery_status.charge_full;
            break;
        case POWER_SUPPLY_PROP_VOLTAGE_MIN_DESIGN:
            val->intval = pi_battery_status.voltage_min_design;
            break;
        case POWER_SUPPLY_PROP_VOLTAGE_NOW:
            val->intval = pi_battery_status.voltage_now;
            break;
        case POWER_SUPPLY_PROP_CURRENT_NOW:
            val->intval = pi_battery_status.current_now;
            break;
        default:
            pr_info("%s: some properties deliberately report errors.\n", __func__);
            return -EINVAL;
    }
    return 0;
}

static int
pi_ac_get_property(struct power_supply *psy,
        enum power_supply_property psp,
        union power_supply_propval *val)
{
    switch (psp) {
        case POWER_SUPPLY_PROP_ONLINE:
            val->intval = ac_status;
            break;
        default:
            return -EINVAL;
    }
    return 0;
}

static int __init
pi_battery_init(void)
{
    int result;
    int i;

    result = misc_register(&control_device);
    if (result) {
        printk(KERN_ERR "Unable to register misc device!");
        return result;
    }

    for (i = 0; i < ARRAY_SIZE(descriptions); i++) {
        supplies[i] = power_supply_register(NULL, &descriptions[i], &configs[i]);
        if (IS_ERR(supplies[i])) {
            printk(KERN_ERR "Unable to register power supply %d in pi_battery\n", i);
            goto error;
        }
    }

    printk(KERN_INFO "loaded pi_battery module\n");
    return 0;

error:
    while (--i >= 0)
        power_supply_unregister(supplies[i]);

    misc_deregister(&control_device);
    return PTR_ERR(supplies[i]);
}

static void __exit
pi_battery_exit(void)
{
    int i;

    for (i = ARRAY_SIZE(descriptions) - 1; i >= 0; i--)
        power_supply_unregister(supplies[i]);

    misc_deregister(&control_device);
    printk(KERN_INFO "unloaded pi_battery module\n");
}

module_init(pi_battery_init);
module_exit(pi_battery_exit);

MODULE_LICENSE("GPL");
