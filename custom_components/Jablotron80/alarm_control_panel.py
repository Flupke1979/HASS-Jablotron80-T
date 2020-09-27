"""This platform enables the possibility to control a Jablotron alarm."""
import logging
import re
import time
import voluptuous as vol
import asyncio
import threading
import json
from datetime import timedelta, datetime

import homeassistant.components.alarm_control_panel as alarm
from homeassistant.const import (
    CONF_CODE, CONF_DEVICE, CONF_NAME, CONF_VALUE_TEMPLATE,
    STATE_ALARM_ARMED_AWAY, STATE_ALARM_ARMED_HOME, STATE_ALARM_ARMED_NIGHT,
    STATE_ALARM_DISARMED, STATE_ALARM_PENDING, STATE_ALARM_ARMING, STATE_ALARM_DISARMING, STATE_ALARM_TRIGGERED,
    ATTR_CODE_FORMAT)
from homeassistant.components.alarm_control_panel.const import (
    SUPPORT_ALARM_ARM_AWAY, SUPPORT_ALARM_ARM_HOME, SUPPORT_ALARM_TRIGGER, SUPPORT_ALARM_ARM_NIGHT
    )
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType, HomeAssistantType
from homeassistant.components.sensor import PLATFORM_SCHEMA

import queue
#import importlib
#import_module('homeassistant.custom_components.jablotron80.ja80')
from .ja80 import JA80
from .ja80 import JA80TConnection
from .ja80 import JA80AlarmStatus
from .ja80 import JA80AlarmTimestamp

_LOGGER = logging.getLogger(__name__)

CONF_SERIAL_PORT = 'serial_port'
CONF_CODE_PANEL_ARM_REQUIRED = 'code_panel_arm_required'
CONF_CODE_PANEL_DISARM_REQUIRED = 'code_panel_disarm_required'
CONF_CODE_ARM_REQUIRED = 'code_arm_required'
CONF_CODE_DISARM_REQUIRED = 'code_disarm_required'
CONF_CODE_SENSOR_NAMES = 'sensor_names'

DEFAULT_NAME = 'Jablotron Alarm'
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_SERIAL_PORT): cv.string,
    vol.Optional(CONF_CODE): cv.string,
    vol.Optional(CONF_CODE_ARM_REQUIRED, default=False): cv.boolean,
    vol.Optional(CONF_CODE_DISARM_REQUIRED, default=True): cv.boolean,
    vol.Optional(CONF_CODE_PANEL_ARM_REQUIRED, default=False): cv.boolean,
    vol.Optional(CONF_CODE_PANEL_DISARM_REQUIRED, default=True): cv.boolean,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_CODE_SENSOR_NAMES, default={}): {int: cv.string},
})

ATTR_CHANGED_BY = "changed_by"
ATTR_CODE_ARM_REQUIRED = "code_arm_required"
ATTR_TRIGGERD_BY = "triggered_by"

JABLOTRON_KEY_MAP = {
    "0": b'\x80',
    "1": b'\x81',
    "2": b'\x82',
    "3": b'\x83',
    "4": b'\x84',
    "5": b'\x85',
    "6": b'\x86',
    "7": b'\x87',
    "8": b'\x88',
    "9": b'\x89',
    "#": b'\x8e',
    "?": b'\x8e',
    "*": b'\x8f'
}

async def async_setup_platform(hass: HomeAssistantType, config: ConfigType,
                               async_add_entities, discovery_info=None):

    async_add_entities([JablotronAlarm(hass, config)])


class JablotronAlarm(alarm.AlarmControlPanelEntity):
    """Representation of a Jabltron alarm status."""

    def __init__(self, hass, config):
        """Init the Alarm Control Panel."""
        self._state = None
        self._sub_state = None
        self._changed_by = None
        self._triggered_by = None
        self._name = config.get(CONF_NAME)
        self._serial_port = config.get(CONF_SERIAL_PORT)
        self._available = False
        self._code = config.get(CONF_CODE)
        self._connection = None  # serial connection handle
        self._hass = hass
        self._system = None
        self._config = config
        self._model = 'Unknown'
        self._lock = threading.BoundedSemaphore()
        self._stop = threading.Event()
        self._updated = asyncio.Event()
        self._desired_state_updated = asyncio.Event()
        self._wait_task = None
        self._command_q = queue.Queue()
        # self._tamper_treshold = config.get(CONF_CODE)
        # self._tamper_window = config.get(CONF_CODE)

        try:
            hass.bus.async_listen('homeassistant_stop', self.shutdown_threads)

            from concurrent.futures import ThreadPoolExecutor
            self._io_pool_exc = ThreadPoolExecutor(max_workers=5)

            self._loop_future = self._io_pool_exc.submit(self._connection_loop)

            self.loop = asyncio.get_running_loop()

            self.loop.create_task(self.state_loop())

        except Exception as ex:
            _LOGGER.error('Unexpected error: %s', format(ex))

    def shutdown_threads(self, event):

        _LOGGER.debug('handle_shutdown() called')

        self._stop.set()
        if self._wait_task is not None:
            self._wait_task.cancel()

        self._command_q.put(None)

        _LOGGER.debug('exiting handle_shutdown()')

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def changed_by(self):
        """Return the last source of state change."""
        return self._changed_by

    @property
    def triggered_by(self):
        """Return the sensor which triggered the alarm"""
        return self._triggered_by

    @property
    def available(self):
        return self._available

    @property
    def code_format(self):
        """Return one or more digits/characters."""
        code = self._code
        if code is None:
            return None

        # Return None if no code needed in HA
        if not self._config[CONF_CODE_ARM_REQUIRED] and not self._config[CONF_CODE_DISARM_REQUIRED]:
            return None

        if isinstance(code, str) and re.search('^\\d+$', code):
            return alarm.FORMAT_NUMBER
        return alarm.FORMAT_TEXT

    @property
    def supported_features(self) -> int:
        """Return the list of supported features."""
        return SUPPORT_ALARM_ARM_HOME | SUPPORT_ALARM_ARM_AWAY | SUPPORT_ALARM_TRIGGER | SUPPORT_ALARM_ARM_NIGHT

    @property
    def state_attributes(self):
        """Return the state attributes."""
        state_attr = {
            ATTR_CODE_FORMAT: self.code_format,
            ATTR_CHANGED_BY: self.changed_by,
            ATTR_CODE_ARM_REQUIRED: self.code_arm_required,
            ATTR_TRIGGERD_BY: self.triggered_by,
        }
        return state_attr

    async def _update(self):

        # _LOGGER.debug('_update called, state: %s', self._state )
        self._updated.set()
        self.async_schedule_update_ha_state()
        # _LOGGER.debug('_update exited, state: %s', self._state )
        
    def _connection_loop(self):

        try:
            # try to create serial connection and provide command queue ref
            self._connection = JA80TConnection(self._serial_port, self._command_q)
            self._connection.connect()
            self._system = JA80()  # holds the JA80 alarm system's specific logic
            self._model = 'Jablotron Oasis JA-82K'

            lastTriggerTime = None

            while not self._stop.is_set():

                # read next packet and send command if we have one queued
                event_data = self._connection.read_send_packet()
                if event_data is False:
                    # error occured during reading, should not happen
                    self._available = False
                    new_state = 'No Signal'
                elif event_data is None:
                    # no event or unrecognised data; ignore and do a new read
                    continue
                else: 
                    self._available = True
                    new_state = self._system.read_state(event_data)
                    if new_state is None:
                        # no state or irrelevant/ignored event, do a new read
                        continue
                    if self._system.sensor_id is not None:
                        self._triggered_by = "%s: %s" % (self._system.sensor_id, self._config[CONF_CODE_SENSOR_NAMES].get(self._system.sensor_id, '?'))

                if new_state != self._state:
                    _LOGGER.info("Jablotron state change detected: %s to %s", self._state, new_state)
                    if new_state == STATE_ALARM_TRIGGERED and self._triggered_by is None:
                        _LOGGER.debug("Alarm triggered but source not known yet")

                        # wait for _triggered_by to be set before returning triggered state, but not more that 10 seconds
                        if lastTriggerTime is not None and (datetime.now() - lastTriggerTime).seconds < 10:
                            continue
                        elif lastTriggerTime is None:
                            lastTriggerTime = datetime.now()
                            continue

                    elif new_state == STATE_ALARM_DISARMED:
                        lastTriggerTime = None  # clear last trigger time
                        self._triggered_by = None  # clear triggered_by
                        self._system.sensor_id = None

                    # Update state & notify home assistant
                    self._state = new_state
                    asyncio.run_coroutine_threadsafe(self._update(), self._hass.loop)

        except Exception as ex:
            _LOGGER.error('Unexpected error: %s', format(ex))

        finally:
            self._connection.close()
            _LOGGER.debug('exiting read_loop()')

    async def async_alarm_disarm(self, code=None):
        """Send disarm command.

        This method is a coroutine.
        """

        if self._config[CONF_CODE_DISARM_REQUIRED] and not self._validate_code(code, 'disarming'):
            return

        send_code = ""
        if self._config[CONF_CODE_PANEL_DISARM_REQUIRED]:
            # Use code from config if and only if none is entered by user and setup as not required
            if code is None and not self._config[CONF_CODE_DISARM_REQUIRED]:
                code = self._code
            send_code = code

        action = "*0"
        if send_code != "":
            # *0 not required if we disarm using code
            action = "#"

        await self._sendCommand(send_code, action, STATE_ALARM_DISARMED)

    async def async_alarm_arm_home(self, code=None):
        """Send arm home command.

        This method is a coroutine.
        """
        if self._config[CONF_CODE_ARM_REQUIRED] and not self._validate_code(code, 'arming home'):
            return

        send_code = ""
        if self._config[CONF_CODE_PANEL_ARM_REQUIRED]:
            send_code = code

        action = "*2"

        await self._sendCommand(send_code, action, STATE_ALARM_ARMED_HOME)

    async def async_alarm_arm_away(self, code=None):
        """Send arm away command.

        This method is a coroutine.
        """
        if self._config[CONF_CODE_ARM_REQUIRED] and not self._validate_code(code, 'arming away'):
            return

        send_code = ""
        if self._config[CONF_CODE_PANEL_ARM_REQUIRED]:
            send_code = code

        action = "*1"

        await self._sendCommand(send_code, action, STATE_ALARM_ARMED_AWAY)

    async def async_alarm_arm_night(self, code=None):
        """Send arm night command.

        This method is a coroutine.
        """
        if self._config[CONF_CODE_ARM_REQUIRED] and not self._validate_code(code, 'arming night'):
            return

        send_code = ""
        if self._config[CONF_CODE_PANEL_ARM_REQUIRED]:
            send_code = code

        action = "*3"

        await self._sendCommand(send_code, action, STATE_ALARM_ARMED_NIGHT)

    async def _sendCommand(self, code, action, desired_state):

        payload = action

        if code is not None and code != "":
            payload += code

        for cmd in payload:
            # self._command_q.put(bytes([cmd]))
            self._command_q.put(JABLOTRON_KEY_MAP.get(cmd))

        self._desired_state = desired_state
        self._changed_by = "hass"

        self._desired_state_updated.set()

        if self._wait_task is not None:
            self._wait_task.cancel()

    async def state_loop(self):

        _LOGGER.debug('state_loop() enter')

        while not self._stop.is_set():

            retrying = False

            await self._desired_state_updated.wait()
            self._desired_state_updated.clear()

            # _LOGGER.debug('command received: %s', self._payload)
            _LOGGER.debug('state change request received: %s', self._desired_state)

            while self.state != self._desired_state:

                self._updated.clear()

                # if not retrying or (self.state != STATE_ALARM_ARMING and self.state != STATE_ALARM_DISARMING and self.state != STATE_ALARM_PENDING) :
                #     await self._send_keys(self._payload)

                try:

                    if self._desired_state == STATE_ALARM_DISARMED:
                        timeout = 10
                    else:
                        timeout = 40

                    self._wait_task = self.loop.create_task(self._updated.wait())
                    await asyncio.wait_for(self._wait_task, timeout)
                    self._updated.clear()

                except asyncio.TimeoutError:
                    _LOGGER.warn('Timed out waiting for change of state, retry')

                except asyncio.CancelledError:
                    _LOGGER.debug('New desired state set, wait has been cancelled, wait for next command')
                    break

                except Exception as ex:
                    _LOGGER.error('Unexpected error: %s', format(ex))
                    break

                retrying = True

                _LOGGER.debug('state: %s', self.state)

        _LOGGER.debug('state_loop(): exit')

    def _validate_code(self, code, state):
        """Validate given code."""
        conf_code = self._code
        check = conf_code is None or code == conf_code
        if not check:
            _LOGGER.warning('Wrong code entered for %s', state)
        return check
