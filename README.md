# HASS-Jablotron80-T
Home Assistant platform to control Jablotron Oasis JA-82K Control Panel via JA-80T Serial USB interface. 
The JA-80T communication protocol is completely different to the JA-82T. The latter uses the HID-standard while the JA-80T might require drivers depending on your distro. This interface does not require a startup message as the control panel is continuously sending data. The first packet might be incomplete for this reason. 
The interface is usually available as serial device via /dev/ttyUSB0.  
 
For 80 series devices with JA-82T please see other repo here https://github.com/mattsaxon/HASS-Jablotron80
For 100 series devices please see other repo here https://github.com/plaksnor/HASS-JablotronSystem

## Supported devices
- Probably any Jablotron Oasis 80 series control panel with JA-80T Serial USB interface.  

## Installation
To use this platform, install pyserial module `pip3 install pyserial`, copy alarm_control_panel.py and ja80.py to "<home assistant config dir>/custom_components/jablotron/" and add the config below to configuration.yaml

```
alarm_control_panel:
  - platform: jablotron
    serial_port: [serial port path]    
    code: [code to send to physical panel and code to enter into HA UI]
    code_panel_arm_required: [True if you need a code to be sent to physical panel on arming, Default False]
    code_panel_disarm_required: [True if you need a code to be sent to physical panel on disarming, Default True]
    code_arm_required: [True if you want a code to need to be entered in HA UI prior to arming, Default False]
    code_disarm_required: [True if you want a code to need to be entered in HA UI prior to disarming, Default True]
    sensor_names: [Optional mapping from sensor ID to name for more user friendly triggered information]
    tamper_threshold: [Optional threshold for tamper alarms Default 0)]
    tamper_window: [Optional time window in minutes for tamper threshold, Default 10]
```
Note: Most of my sensors have unreliable tamper switches that are triggered randomly, likely because of the age of my system. This has caused some false alarms which is frustrating. I don't care about 1 tamper event in a 10 minute time window and setting config to tamper_threshold: 1 and tamper_window: 10 will automatically cancel tamper alarms if we only see 1 event in 10 minutes. This is implemented by disarming the system and rearming it again. A lower priority alert will be send. If we see another tamper alarm in the same 10 minute time window from a different sensor, the alarm will not be cancelled.

Example:
```
alarm_control_panel:
  - platform: jablotron
    serial_port: /dev/ttyUSB0     
    code: !secret alarm_code
    code_panel_arm_required: False
    code_panel_disarm_required: True
    code_arm_required: False
    code_disarm_required: False
    sensor_names: 
      1: "Front door"
      2: "Garden door"
      3: "Bedroom"
```

Note 1: Use the following command line to identity your device:

```
$ dmesg | tail
usb 2-2.1: new full-speed USB device number 8 using uhci_hcd
usb 2-2.1: New USB device found, idVendor=16d6, idProduct=0001, bcdDevice= 1.00
usb 2-2.1: New USB device strings: Mfr=1, Product=2, SerialNumber=3
usb 2-2.1: Product: JABLOTRON serial interface
usb 2-2.1: Manufacturer: Silicon Labs
usb 2-2.1: SerialNumber: 1
cp210x 2-2.1:1.0: cp210x converter detected
usb 2-2.1: cp210x converter now attached to ttyUSB0
```

Note 2: if you supply a code, this is used as the default code to arm/disarm it.  

## Usage in automation
With the following automation setup, you'll get a notification when alarm is triggerd with the id and name (if you configured sensor_names) of the sensor that triggered it.

```
  trigger:
  - entity_id: alarm_control_panel.jablotron_alarm
    platform: state
    to: triggered
  condition: []
  action:
  - data_template:
      message: ALARM! {{ trigger.to_state.attributes.triggered_by }}
    service: notify.notify
```

## Debug
If you have issues with this, I'd be happy to work with you on improving the code but please do your research first (Google it!).  
Please include a detailed description of the issue, what you've tried so far and the relevant logs files (please remove any sensitive/PIN data before sharing). 
  
Change configuration.yaml to include:

```
logger:
  logs:
    custom_components.jablotron: debug
```

## Other Info
There is a thread discussing this integration [here](https://community.home-assistant.io/t/jablotron-ja-80-series-and-ja-100-series-alarm-integration/113315/3), however for issues, please raise the issue in this GitHub repo. 

## What if my HA instance isn't near my alarm control panel?
I might develop a stand-alone application and use MQTT for comms with HA later but you could also use some fancy socat plumbing to share the serial connection over IP.

## Feature requests
Please raise as an issue.