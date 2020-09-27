import serial
import logging
import queue
import time
from datetime import datetime

from homeassistant.const import (
    CONF_CODE, CONF_DEVICE, CONF_NAME, CONF_VALUE_TEMPLATE,
    STATE_ALARM_ARMED_AWAY, STATE_ALARM_ARMED_HOME, STATE_ALARM_ARMED_NIGHT,
    STATE_ALARM_DISARMED, STATE_ALARM_PENDING, STATE_ALARM_ARMING, STATE_ALARM_DISARMING, STATE_ALARM_TRIGGERED,
    ATTR_CODE_FORMAT)

_LOGGER = logging.getLogger(__name__)

class SerialMock():

    mock_data = []
    dummy_data = 'ed 40 00 00 30 00 00 00 60 ff'
    data_buffer = []

    def __init__(self, device, test_data=None):
        _LOGGER.info('SerialMock:init for device %s', device)
        if test_data is not None:
            self.mock_data = test_data

    def flush(self):
        _LOGGER.info('SerialMock:flush')

    def close(self):
        _LOGGER.info('SerialMock:close')

    def is_open(self):
        return True

    def read(self):

        if len(self.data_buffer) == 0:
            #_LOGGER.info('SerialMock:read init data buffer')
            if len(self.mock_data) == 0:
                self.mock_data.append(self.dummy_data)

            # translate mock data to simulate data from serial connection
            for event in self.mock_data:
                # translate back to individual bytes
                event_bytes = event.split()
                for event_byte in event_bytes:
                    self.data_buffer.append(bytes([int(event_byte, 16)]))

        data = self.data_buffer.pop(0)
        # _LOGGER.info('SerialMock:read %s', data)
        return data

    def write(self, buf):
        _LOGGER.info('SerialMock:write %s (%s)', buf, len(buf))

        # we expect confirmation, so add to mock data
        self.data_buffer.append(buf)
        self.data_buffer.append(bytes([0xff]))
        return len(buf)


class JA80AlarmStatus:
    msg_raw = msg_type = alarm_status = message_id = device_id = None
    unknown_val = None 
    device_name = device_type = ''
    alarm_state = None
    led_a = led_b = led_c = led_backlight = led_warning = False

    ALARM_STATE_DISARMED = 0x00
    ALARM_STATE_ARMED = 0x02
    ALARM_STATE_ALARM = 0x04
    ALARM_STATE_ENTRY_DELAY = 0x08
    ALARM_STATE_EXIT_DELAY = 0x10

    def __init__(self, msg):
        self.parse_msg(msg)

    def parse_msg(self, msg):

        if len(msg) != 10:
            raise ValueError('Invalid msg len', len(msg), '(expect 10)')
        self.msg_raw = msg

        '''
        format:
        byte
         0 = msg type 0xed
         1 = alarm_status
         2 = msg_id
         3 = device_id
         4 = leds
         5-7 = unknown (display content, signal strength, zone)
         8 = checksum
         9 = 0xFF end of message
        '''
        self.msg_type = msg[0]
        self.set_alarm_status(msg[1])
        self.message_id = msg[2]
        self.set_device(msg[3])  # translate id to device type and name
        self.set_leds(msg[4])
        self.unknown_val = msg[7]  # still need to figure out what this is / might be some device message/ motion/tamper

    def set_alarm_status(self, alarm_status):

        self.raw_status = alarm_status
        if (alarm_status & 0x1f) == self.ALARM_STATE_DISARMED:
            self.alarm_status = self.ALARM_STATE_DISARMED

        elif (alarm_status & 0x04) == self.ALARM_STATE_ALARM:
            self.alarm_status = self.ALARM_STATE_ALARM

        elif (alarm_status & 0x08) == self.ALARM_STATE_ENTRY_DELAY:
            self.alarm_status = self.ALARM_STATE_ENTRY_DELAY

        elif (alarm_status & 0x10) == self.ALARM_STATE_EXIT_DELAY:
            self.alarm_status = self.ALARM_STATE_EXIT_DELAY

        else:
            self.alarm_status = self.ALARM_STATE_ARMED

    def get_alarm_status_name(self, alarm_status=None):

        if alarm_status is None:
            alarm_status = self.alarm_status

        if alarm_status == self.ALARM_STATE_DISARMED:
            return 'Disarmed'

        elif alarm_status == self.ALARM_STATE_ALARM:
            return 'Alarm'

        elif alarm_status == self.ALARM_STATE_ENTRY_DELAY:
            return 'Entry delay'

        elif alarm_status == self.ALARM_STATE_EXIT_DELAY:
            return 'Exit delay'

        return 'Armed'

    def get_hass_status(self, alarm_status=None):
        # translate JA status to Home Assistant status

        if alarm_status is None:
            alarm_status = self.alarm_status

        if alarm_status == self.ALARM_STATE_DISARMED:
            return STATE_ALARM_DISARMED
        elif alarm_status == self.ALARM_STATE_ALARM:
            return STATE_ALARM_TRIGGERED
        elif alarm_status == self.ALARM_STATE_ENTRY_DELAY:
            return STATE_ALARM_DISARMING
        elif alarm_status == self.ALARM_STATE_EXIT_DELAY:
            return STATE_ALARM_ARMING
        elif alarm_status == self.ALARM_STATE_ARMED:
            return STATE_ALARM_ARMED_AWAY

        return 'Unknown'

    def set_leds(self, led_status):

        #  led bits:
        self.led_a = ((led_status & 0x08) == 0x08)
        self.led_b = ((led_status & 0x04) == 0x04)
        self.led_c = ((led_status & 0x02) == 0x02)
        self.led_backlight = ((led_status & 0x01) == 0x01)
        self.led_warning = ((led_status & 0x10) == 0x10)

    def set_device(self, device_id):
        #  @TODO: mapping from id to device details
        self.device_id = device_id
        self.device_name = 'unknown'
        self.device_type = 'unknown'

    def __str__(self):

        s = 'AlarmStatus: msg_type = ' + '0x{:02x}'.format(self.msg_type) + '\n'
        s += f'    alarm_status = {self.get_alarm_status_name(self.alarm_status)} / ' + '0x{:02x}'.format(self.raw_status)
        s += f'    message_id = {self.message_id}'
        s += f'    device_id = {self.device_id}'
        s += f'    unknown_val = ' + '0x{:02x}'.format(self.unknown_val) + '\n'
        s += f'    leds: a={self.led_a}, b={self.led_b}, c={self.led_c}, backlight={self.led_backlight}, warning={self.led_warning}'
        return s


class JA80TConnection():

    mock = False
    test_data = None

    device = None
    connection = None

    cmd_q = None
    cmd_confirm_pending = None

    # device is mandatory at initiation
    def __init__(self, device, cmd_q, mock=False, test_data=None):
        if mock:
            device = '/mock'
            self.mock = True
            self.test_data = test_data
        _LOGGER.info('Init JA80TConnection with device %s', device)
        self.device = device
        self.cmd_q = cmd_q

    def connect(self):
        _LOGGER.info('Connecting to JA80 via JA-80T using %s...', self.device)
        if self.mock:
            self.connection = SerialMock(self.device, self.test_data)
        else:
            self.connection = serial.Serial(
                port=self.device,
                baudrate=9600,
                parity=serial.PARITY_NONE,
                bytesize=serial.EIGHTBITS,
                dsrdtr=True,
                # stopbits=serial.STOPBITS_ONE
                timeout=1)

    def disconnect(self):
        if self.is_connected():
            _LOGGER.info('Disconnecting from JA80...')
            self.connection.flush()
            self.connection.close()
        else:
            _LOGGER.info('No need to disconnect; not connected')

    def is_connected(self):
        return self.connection is not None and self.connection.is_open

    def get_command(self):

        # assume we have a command queue and return command if requested
        if self.cmd_q.empty():
            return None
        try:
            # get one command
            cmd = self.cmd_q.get_nowait()
            if cmd is not None:
                # we could postpone this until command has been confirmed
                self.cmd_q.task_done()
        except queue.Empty:
            _LOGGER.info('All command queue items processed')
            pass
        return cmd

    def read_send_packet(self):
        # keep reading bytes untill 0xff which indicates end of packet
        _LOGGER.info('Read until we have captured one packet and return data')
        if not self.is_connected():
            _LOGGER.warning('Not connected to JA80, abort')
            return False

        retry_limit = 5
        retries = 0
        max_package_length = 15  # longest packet seen is 10 bytes: ed 53 0c 00 3e 04 00 28 0b ff
        read_buffer = []
        for i in range(max_package_length):

            data = self.connection.read()
            if len(data) == 0:
                retries += 1
                if retries < retry_limit:
                    _LOGGER.info('No data received, retry in 1 second')
                    time.sleep(1)
                    continue
                else:
                    _LOGGER.warning('No data received after %s retries, abort', retry_limit)
                    return False

            retries = 0

            data_dec = ord(data)
            read_buffer.append(data_dec)

            if data_dec == 0xff:
                # end of this packet, check for command confirmation and then handle data
                if self.cmd_confirm_pending is not None:
                    # print('Pending last command confirmation, buf len', len(read_buffer), 'buf 0', read_buffer[0], 'cmd p', ord(self.cmd_confirm_pending))
                    # see if current buffer matches command
                    if len(read_buffer) == 2 and read_buffer[0] == ord(self.cmd_confirm_pending):
                        _LOGGER.info('Last command confirmed')
                        self.cmd_confirm_pending = None
                
                # see if there is a new command we need to send
                # only continue if we are not waiting for a confirmation of last command
                if self.cmd_confirm_pending is None:

                    send_cmd = self.get_command()
                    if send_cmd is not None:
                        # can only send one command at a time, wait for the command to be reflected back and then send next one
                        _LOGGER.info('New command, send to JA80... %s', send_cmd)

                        self.cmd_confirm_pending = send_cmd
                        data_written = self.connection.write(send_cmd)
                        # _LOGGER.info('Command sent, return %s', data_written)

                # return data we read earlire
                return read_buffer

        # finished reading data for max package length without package end marker 0xff
        return False


class JA80AlarmTimestamp:
    msg_raw = timestamp = event_type = event_source = None

    EVENT_MOTION_ALARM = 0x01  # ?? seen when alarm is triggered via motion (but might be same for door)
    EVENT_OTHER_ALARM2 = 0x02
    EVENT_OTHER_ALARM3 = 0x03
    EVENT_OTHER_ALARM4 = 0x04
    EVENT_TAMPER_ALARM = 0x05
    EVENT_ARMING = 0x08  # arming request
    EVENT_DISARMING = 0x09 # disarming request
    EVENT_ARMING_KEYPAD = 0x0c  ## ?? seen when armed via keypad (maybe in tamper state?)
    EVENT_TAMPER_SENSORS_OK = 0x50
    EVENT_CANCEL_ALARM = 0x4e  # ?? seen when system is disarmed when alarm is active

    # these will triger prio 1 alerts (intrusion)
    alarm_status = [EVENT_MOTION_ALARM, EVENT_OTHER_ALARM2, EVENT_OTHER_ALARM3, EVENT_OTHER_ALARM4]

    '''
    e3 02 01 23 36 08 09 3f ff
    alarm time stamp event, here 02-01 23:36 (d-m h:i) event type 08 source 09                  
                event type 08 = Setting 
    53 S    arming          source 09 = keyfob (in my case)
    '''
    def __init__(self, msg):
        self.parse_msg(msg)

    def is_alarm(self):
        return self.event_type in self.alarm_status

    def parse_msg(self, msg):
        if len(msg) != 9:
            raise ValueError('Invalid msg len', len(msg), '(expect 10)')
        self.msg_raw = msg
        # these are binary coded (16 hex = 16 dec) so print hex values
        self.timestamp = '{:02x}'.format(msg[1]) + '/' + '{:02x}'.format(msg[2]) + ' ' + '{:02x}'.format(msg[3]) + ':' + '{:02x}'.format(msg[4])
        self.event_type = msg[5]
        self.event_name = self.get_event_type_name(msg[5])
        # self.event_source = int(msg[6], 16)
        self.event_source = msg[6]  # eg 49 for keypad, 9 = keyfob

    def get_event_type_name(self, event_type=None):

        if event_type is None:
            event_type = self.event_type

        if event_type == self.EVENT_MOTION_ALARM:
            return 'Motion alarm'
        elif (event_type == self.EVENT_OTHER_ALARM2
            or event_type == self.EVENT_OTHER_ALARM3 
            or event_type == self.EVENT_OTHER_ALARM4):
            return 'Other alarm'
        elif event_type == self.EVENT_TAMPER_ALARM:
            return 'Tamper alarm'
        elif event_type == self.EVENT_ARMING:
            return 'Arming via keyfob'
        elif event_type == self.EVENT_ARMING_KEYPAD:
            return 'Arming via keypad'
        elif event_type == self.EVENT_DISARMING:
            return 'Disarming'
        elif event_type == self.EVENT_TAMPER_SENSORS_OK:
            return 'All tamper sensors ok'
        elif event_type == self.EVENT_CANCEL_ALARM:
            return 'Cancel alarm'
        return 'Unknown alarm event'

    def get_hass_status(self, event_type=None):

        if event_type is None:
            event_type = self.event_type

        if event_type == self.EVENT_MOTION_ALARM:
            return STATE_ALARM_TRIGGERED
        elif (event_type == self.EVENT_OTHER_ALARM2
            or event_type == self.EVENT_OTHER_ALARM3 
            or event_type == self.EVENT_OTHER_ALARM4):
            return STATE_ALARM_TRIGGERED
        elif event_type == self.EVENT_TAMPER_ALARM:
            return 'STATE_TAMPER_ALARM_TRIGGERED'
        elif event_type == self.EVENT_ARMING:
            return STATE_ALARM_ARMING
        elif event_type == self.EVENT_ARMING_KEYPAD:
            return STATE_ALARM_ARMING
        elif event_type == self.EVENT_DISARMING:
            return STATE_ALARM_DISARMING
        elif event_type == self.EVENT_TAMPER_SENSORS_OK:
            return 'STATE_TAMPER_SENSORS_OK'
        elif event_type == self.EVENT_CANCEL_ALARM:
            return 'CANCEL_ALARM'
        return 'STATE_UNKNOWN'

    def __str__(self):

        s = 'AlarmTimestamp:\n'
        s += f'    timestamp = {self.timestamp}\n'
        s += f'    event_type = {self.event_name} ({self.event_type})\n'
        s += f'    event_source = {self.event_source}'
        return s


class JA80(object):

    current_alarm_status = None
    sensor_id = None
    # last_tamper_event = 
    # tamper_event_count_since_last

    MSG_TYPE_KEYPRESS = 'KeyPress'
    MSG_TYPE_BEEP = 'Beep'
    MSG_TYPE_ALARM_STATUS = 'AlarmStatus'
    MSG_TYPE_ALARM_TIMESTAMP = 'AlarmTimestamp'
    MSG_TYPE_STATE_STATUS = 'StateStatus'

    CMD_DISARM_SYSTEM = 1
    CMD_LONG_BEEP = 2
    CMD_SHORT_BEEP = 3
    CMD_ARM_SYSTEM = 4
    #CMD_CANCEL_ALARM = 5

    msg_types = {
         0x80: MSG_TYPE_KEYPRESS
        ,0xa0: MSG_TYPE_BEEP
        ,0xed: MSG_TYPE_ALARM_STATUS
        ,0xe3: MSG_TYPE_ALARM_TIMESTAMP
        ,0xe8: MSG_TYPE_STATE_STATUS
    }

    keypress_options = {
         0x0: {'val': '0', 'desc': 'Key 0 pressed on keypad'}
        ,0x1: {'val': '1', 'desc': 'Key 1 (^) pressed on keypad'}
        ,0x2: {'val': '2', 'desc': 'Key 2 pressed on keypad'}
        ,0x3: {'val': '3', 'desc': 'Key 3 pressed on keypad'}
        ,0x4: {'val': '4', 'desc': 'Key 4 (<) pressed on keypad'}
        ,0x5: {'val': '5', 'desc': 'Key 5 pressed on keypad'}
        ,0x6: {'val': '6', 'desc': 'Key 6 (>) pressed on keypad'}
        ,0x7: {'val': '7', 'desc': 'Key 7 (v) pressed on keypad'}
        ,0x8: {'val': '8', 'desc': 'Key 8 pressed on keypad'}
        ,0x9: {'val': '9', 'desc': 'Key 9 pressed on keypad'}
        #,0xa: {'val': 'A', 'desc': 'Key A pressed on keypad'} A, B, ABC keys appear to be shortcuts for *1, *2, *3
        #,0xb: {'val': 'B', 'desc': 'Key B pressed on keypad'}
        #,0xc: {'val': 'C', 'desc': 'Key ABC pressed on keypad'}
        #,0xd: {'val': '?', 'desc': 'Key ? pressed on keypad'} ? will just send # (8E)
        ,0xe: {'val': '#', 'desc': 'Key # (ESC/OFF) pressed on keypad'}
        ,0xf: {'val': '*', 'desc': 'Key * (ON) pressed on keypad'}
    }

    beep_options = {
         0x0: {'val': '1s', 'desc': '1 subtle (short) beep triggered'}
        ,0x1: {'val': '1l', 'desc': '1 loud (long) beep triggered'}
        ,0x2: {'val': '2l', 'desc': '2 loud (long) beeps triggered'}
        ,0x3: {'val': '3l', 'desc': '3 loud (long) beeps triggered'}
        ,0x4: {'val': '4s', 'desc': '4 subtle (short) beeps triggered'}  # happens when warning appears on keypad (e.g. after alarm)
        ,0x8: {'val': 'in', 'desc': 'Infinite beeping triggered'}
        #  ,0x4: {'val': 'o4', 'desc': 'Other beep triggered (4)'}
        #  ,0x5: {'val': 'o5', 'desc': 'Other beep triggered (5)'}
        #  ,0x6: {'val': 'o6', 'desc': 'Other beep triggered (6)'}
        #  ,0x7: {'val': 'o7', 'desc': 'Other beep triggered (7)'}
        #  ,0x9: {'val': 'o9', 'desc': 'Other beep triggered (9)'}
        #  ,0xa: {'val': 'oa', 'desc': 'Other beep triggered (10)'}
    }

    def __init__(self):
        pass

    def read_state(self, buf):

        packet_data = " ".join(["%02x" % c for c in buf])

        # parse data, based on message type (first byte)
        msg_type = None
        try:
            msg_type = self.msg_types.get(buf[0])
            if msg_type is None:
                # try again with only highest 4 bits (e.g. 0x85 > 0x80)
                msg_type = self.msg_types.get(buf[0] & 0xf0)
        except Exception as ex:
            _LOGGER.error('Error determining msg type from buffer: %s', ex)
            #  msg type is still none so next call will work
            pass
        # print(msg_type)
        try:
            if msg_type is None:
                # unknown type
                _LOGGER.info("%s Unimplemented message type | %s", datetime.now(), packet_data)
                return None

            elif msg_type == self.MSG_TYPE_KEYPRESS:
                # 0x0: {'val': '0', 'desc': 'Key 0 pressed on keypad'}
                # unly use lower 4 bits
                key = self.keypress_options.get(buf[0] & 0x0f)
                _LOGGER.info('%s %s', datetime.now(), f"KeyPress: {key} | {packet_data}")
                return None  # ignore this event

            elif msg_type == self.MSG_TYPE_BEEP:
                # 0x1: {'val': '1l', 'desc': '1 loud (long) beep triggered'}
                # unly use lower 4 bits
                beep = self.beep_options.get(buf[0] & 0x0f)
                beep_desc = 'unknown'
                if beep:
                    beep_desc = beep['desc']
                _LOGGER.info('%s %s', datetime.now(), f"Beep: {beep_desc} | {packet_data}")
                return None  # ignore this event

            elif msg_type == self.MSG_TYPE_ALARM_STATUS:
                status = JA80AlarmStatus(buf)
                _LOGGER.info('%s %s', datetime.now(), f"AlarmStatus: {status} | {packet_data}")
                return status.get_hass_status()

            elif msg_type == self.MSG_TYPE_ALARM_TIMESTAMP:
                status = JA80AlarmTimestamp(buf)
                _LOGGER.info('%s %s', datetime.now(), f"AlarmEvent: {status} | {packet_data}")
                self.sensor_id = status.event_source
                return status.get_hass_status()

                # if status.event_type == JA80AlarmTimestamp.EVENT_TAMPER_ALARM:
                #     # cancel alarm if this is a tamper alarm
                #     # TODO: log last_tamper and increment tamper count (in all states)
                    
                #     if self.current_alarm_status == JA80AlarmStatus.ALARM_STATE_ALARM:
                #         print('TODO: CANCEL this tamper alarm if below threshold')
                #     else:
                #         print('Tamper warning (disarmed)', status.event_source)

            elif msg_type == self.MSG_TYPE_STATE_STATUS:
                
                _LOGGER.info('%s %s', datetime.now(), "State status " + '{:02x}'.format(buf[1]) + ' ' + '{:02x}'.format(buf[2]) + f' | {packet_data}')
                return None

        except Exception as ex:
            _LOGGER.error('Exception in handling msg_type %s %s %s', msg_type, ex, packet_data)
            return False

        return None
