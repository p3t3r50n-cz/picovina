# Armbian Postinstall

Post-installation steps after a fresh Armbian setup.

## Device Tree Overlays

### USB Host Fix

The default DTS for Orange Pi 5 MAX on Armbian configures one of the USB ports for USB-OTG mode, which is not suitable for my project. Fortunately, it is easy to revert it back to USB-host mode using a custom overlay.

Create the file `rk3588-usb-host-fix.dts` with the following content:

```
/dts-v1/;
/plugin/;

/ {
    compatible = rockchip,rk3588-orangepi-5-max;

    fragment@0 {
        target-path = /usbdrd3_0/usb@fc000000;
        __overlay__ {
            dr_mode = host;
        };
    };
};
```

See file: [rk3588-usb-host-fix.dts](./user_overlays/rk3588-usb-host-fix.dts)

Then compile it:

```bash
mkdir -p /boot/overlay-user
dtc -@ -I dts -O dtb -o /boot/overlay-user/rk3588-usb-host-fix.dtbo rk3588-usb-host-fix.dts
```

Finally, update `/boot/armbianEnv.txt` by adding to `user_overlays`:

```
user_overlays=rk3588-usb-host-fix
```

---

## HDMI Screen Rotation

In my project, I'm using a **DFRobot 5.5" screen** connected via HDMI. Unfortunately, the screen is originally oriented in portrait mode, but I need it in landscape.  
Luckily, this can be easily configured.

### Rotation Before Xorg

Add extra arguments to `/boot/armbianEnv.txt`:

```
video=HDMI-A-1:1080x1920@60,panel_orientation=left
```

Your `extraargs` line can look like this:

```bash
extraargs=cma=256M video=HDMI-A-1:1080x1920@60,panel_orientation=left console=tty1 splash quiet plymouth.ignore-serial-consoles
```

### Screen Rotation in Xorg

To rotate the screen in Xorg, create the file `/etc/X11/xorg.conf.d/10-screen-rotate.conf` with the following content:

```
Section Monitor
    Identifier HDMI-1         # Output name (check using xrandr)
    Option     Rotate left    # Options: normal, left, right, inverted
EndSection
```

See file: [10-screen-rotate.conf](./10-screen-rotate.conf)

### Touch Rotation in Xorg

The display is also touch-capable, so rotating the touch input is recommended. Create the file `/etc/X11/xorg.conf.d/11-touch-rotate.conf` with the following content:

```
Section InputClass
    Identifier         Touchscreen Calibration
    MatchProduct       DFRobot USB Multi Touch V3.0  # check using lsusb
    MatchIsTouchscreen on
    Driver             libinput
    Option             CalibrationMatrix 0 -1 1 1 0 0 0 0 1
EndSection
```

See file: [11-touch-rotate.conf](./11-touch-rotate.conf)

## Screen Scaling

The 5.5" display with 1920x1080 resolution looks really nice, but it doesn't play well with the small screen size and desktop workspace. Therefore, scaling needs to be addressed.

Since my project uses the good old gold **Trinity** desktop and the **TDM display manager**, which relies on Xorg, we can't use Wayland features - but that's fine, it can still be handled.

Xorg itself doesn’t provide a configuration option for display scaling or transformation (except for some Nvidia cards that might support it), but we can apply a small hack in the `/etc/trinity/tdm/Xsetup` file using `xrandr` to scale the display.

Because TDM reads the **real** resolution, not the **scaled** one, the login dialog initially aligns to the bottom right (it tries to center based on the original resolution). We can fix this by also modifying `/etc/trinity/tdm/Xsetup` and using the `wmctrl` utility to nicely center the login dialog.

**NOTE:** The `/etc/trinity/tdm/Xsetup` script uses some Bash-specific features that plain `sh` doesn’t support. Therefore, change the shebang from `#!/bin/sh` to `#!/bin/bash`.

See file: [Xsetup](./Xsetup)

Installation is very easy — just copy `Xsetup` to `/etc/trinity/tdm/Xsetup` (make sure you have a backup of the original file):

```bash
mv /etc/trinity/tdm/Xsetup /etc/trinity/tdm/Xsetup.bak
cp Xsetup /etc/trinity/tdm/Xsetup
```

## After every armbian kernel update...

... is lost DKMS package for `bcmdhd-sdio` wireless driver. Luckile re-adding wifi support is very easy:

```bash
dkms install -m bcmdhd-sdio -v 101.10.591.52.27-5
```

And from now you have wifi again ;-) You can check with:
```bash
dkms status
```

resulting in:
```bash
bcmdhd-sdio/101.10.591.52.27-5, 6.1.115-vendor-rk35xx, aarch64: installed
```

Note: There are `bcmdhd-sdio` sources in `/usr/src/bcmdhd-sdio-101.10.591.52.27-5` with `dkms.conf`
