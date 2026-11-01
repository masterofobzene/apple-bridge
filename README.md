# Apple-Bridge
A simple graphical program to download, upload and delete videos and pictures from your ios6 device without Itunes for Windows.

<img width="500" alt="XUl5jfw413" src="https://github.com/user-attachments/assets/d6123e82-a29b-4b3c-9256-403f29afcb67" />




Features:

- Upload videos and pics to the ipad/iphone (tested only on ios6)
- Media will be shown on the apple's camera roll (photos app)
- No risk of deleting anything with forced synchronizations
- Download files already uploaded back to your pc
- Delete files you no longer want with a much easier interface and selection mechanics
- No more need to be an itunes slave anymore

-----------------

### Pre-requirements (device & setup)

- Jailbroken iPad on **iOS 6** (may work on iphones too)
- **Apple File Conduit 2** service enabled (install from cydia on jailbroken devices)
- **OpenSSH** installed on the device (install from cydia on jailbroken devices)
- Root password `alpine` (default) — otherwise edit `trigger_ssh_rebuild()` (default when installing openssh)
- USB cable + trusted pairing
- **Windows 10** (11 not tested) with iTunes desktop installed (Apple Mobile Device Service) (but itunes is not used afterwards)
- **Python 3.9+**
- **FFmpeg** with `libx264` + native `aac` encoder (for video re-encoding) on your PATH env. variables.



### Python Install (you need this too)

```
pip install pymobiledevice3 paramiko pillow pillow-heif tkinterdnd2
```
-----------------

### Usage:

1- Plug the device and trust the connection

2- Open the app, it should detect the iDevice automatically

3- Drag and drop your pics/videos to the app window

4- After you loaded all your files, hit "Reboot" (never skip this last step)

-----------------

### DISCLAIMERS / CREDITS:

I am not responsible. In general.
But this time I am not responsible for anything that happens to your files, devices or anything. If you use this
software, you understand this and accept the risks if any. No software is bullet-proof.

This app uses:
https://libimobiledevice.org/

All credits to each dev involved in the creation/maintainance of libimobiledevice.

