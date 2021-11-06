"""Support for Rfplayer devices."""
import asyncio
from collections import defaultdict
import copy
import logging

import async_timeout
from serial import SerialException
import voluptuous as vol

from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_COMMAND,
    CONF_DEVICE,
    CONF_DEVICES,
    EVENT_HOMEASSISTANT_STOP,
    STATE_ON,
)
from homeassistant.core import CoreState, callback
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import *
from .rflib.rfpprotocol import create_rfplayer_connection

_LOGGER = logging.getLogger(__name__)


SEND_COMMAND_SCHEMA = vol.Schema(
    {vol.Required(CONF_DEVICE_ID): cv.string, vol.Required(CONF_COMMAND): cv.string}
)


def identify_event_type(event):
    """Look at event to determine type of device.

    Async friendly.
    """
    if EVENT_KEY_COMMAND in event:
        return EVENT_KEY_COMMAND
    if EVENT_KEY_SENSOR in event:
        return EVENT_KEY_SENSOR
    return "unknown"


async def async_setup_entry(hass, entry):
    """Set up GE RFPlayer from a config entry."""
    config = entry.data

    # Allow entities to register themselves by device_id to be looked up when
    # new rfplayer events arrive to be handled
    hass.data[DATA_ENTITY_LOOKUP] = {
        EVENT_KEY_COMMAND: defaultdict(list),
        EVENT_KEY_SENSOR: defaultdict(list),
    }
    hass.data[DATA_ENTITY_GROUP_LOOKUP] = {EVENT_KEY_COMMAND: defaultdict(list)}

    # Allow platform to specify function to register new unknown devices
    hass.data[DATA_DEVICE_REGISTER] = {}

    async def async_send_command(call):
        """Send Rfplayer command."""
        _LOGGER.debug("Rfplayer command for %s", str(call.data))
        if not (
            await RfplayerCommand.send_command(
                call.data.get(CONF_DEVICE_ID), call.data.get(CONF_COMMAND)
            )
        ):
            _LOGGER.error("Failed Rfplayer command for %s", str(call.data))
        else:
            async_dispatcher_send(
                hass,
                SIGNAL_EVENT,
                {
                    EVENT_KEY_ID: call.data.get(CONF_DEVICE_ID),
                    EVENT_KEY_COMMAND: call.data.get(CONF_COMMAND),
                },
            )

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_COMMAND, async_send_command, schema=SEND_COMMAND_SCHEMA
    )

    @callback
    def event_callback(event):
        """Handle incoming Rfplayer events.

        Rfplayer events arrive as dictionaries of varying content
        depending on their type. Identify the events and distribute
        accordingly.
        """
        event_type = identify_event_type(event)
        _LOGGER.debug("event of type %s: %s", event_type, event)

        # Don't propagate non entity events (eg: version string, ack response)
        if event_type not in hass.data[DATA_ENTITY_LOOKUP]:
            _LOGGER.debug("unhandled event of type: %s", event_type)
            return

        # Lookup entities who registered this device id as device id or alias
        event_id = event.get(EVENT_KEY_ID)

        is_group_event = (
            event_type == EVENT_KEY_COMMAND
            and event[EVENT_KEY_COMMAND] in RFPLAYER_GROUP_COMMANDS
        )
        if is_group_event:
            entity_ids = hass.data[DATA_ENTITY_GROUP_LOOKUP][event_type].get(
                event_id, []
            )
        else:
            entity_ids = hass.data[DATA_ENTITY_LOOKUP][event_type][event_id]

        _LOGGER.debug("entity_ids: %s", entity_ids)
        if entity_ids:
            # Propagate event to every entity matching the device id
            for entity in entity_ids:
                _LOGGER.debug("passing event to %s", entity)
                async_dispatcher_send(hass, SIGNAL_HANDLE_EVENT.format(entity), event)
        elif not is_group_event:
            # If device is not yet known, register with platform (if loaded)
            if event_type in hass.data[DATA_DEVICE_REGISTER]:
                _LOGGER.debug("device_id not known, adding new device")
                # Add bogus event_id first to avoid race if we get another
                # event before the device is created
                # Any additional events received before the device has been
                # created will thus be ignored.
                hass.data[DATA_ENTITY_LOOKUP][event_type][event_id].append(
                    TMP_ENTITY.format(event_id)
                )
                _add_device(event, event_id)
                hass.async_create_task(
                    hass.data[DATA_DEVICE_REGISTER][event_type](event)
                )
            else:
                _LOGGER.debug("device_id not known and automatic add disabled")

    @callback
    def _add_device(event, event_id):
        """Add a device to config entry."""
        data = entry.data.copy()
        data[CONF_DEVICES] = copy.deepcopy(entry.data[CONF_DEVICES])
        data[CONF_DEVICES][event_id] = event
        hass.config_entries.async_update_entry(entry=entry, data=data)

    @callback
    def reconnect(exc=None):
        """Schedule reconnect after connection has been unexpectedly lost."""
        # Reset protocol binding before starting reconnect
        RfplayerCommand.set_rfplayer_protocol(None)

        async_dispatcher_send(hass, SIGNAL_AVAILABILITY, False)

        # If HA is not stopping, initiate new connection
        if hass.state != CoreState.stopping:
            _LOGGER.warning("Disconnected from Rfplayer, reconnecting")
            hass.async_create_task(connect())

    async def connect():
        """Set up connection and hook it into HA for reconnect/shutdown."""
        _LOGGER.info("Initiating Rfplayer connection")
        connection = create_rfplayer_connection(
            port=config[CONF_DEVICE],
            event_callback=event_callback,
            disconnect_callback=reconnect,
            loop=hass.loop,
            ignore=config.get(CONF_IGNORE_DEVICES),
        )

        try:
            with async_timeout.timeout(CONNECTION_TIMEOUT):
                transport, protocol = await connection

        except (
            SerialException,
            OSError,
            asyncio.TimeoutError,
        ) as exc:
            reconnect_interval = config[CONF_RECONNECT_INTERVAL]
            _LOGGER.exception(
                "Error connecting to Rfplayer, reconnecting in %s", reconnect_interval
            )
            # Connection to Rfplayer device is lost, make entities unavailable
            async_dispatcher_send(hass, SIGNAL_AVAILABILITY, False)

            hass.loop.call_later(reconnect_interval, reconnect, exc)
            return

        # There is a valid connection to a Rfplayer device now so
        # mark entities as available
        async_dispatcher_send(hass, SIGNAL_AVAILABILITY, True)

        # Bind protocol to command class to allow entities to send commands
        RfplayerCommand.set_rfplayer_protocol(protocol, config[CONF_WAIT_FOR_ACK])

        # handle shutdown of Rfplayer asyncio transport
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, lambda x: transport.close()
        )

        _LOGGER.info("Connected to Rfplayer")

    hass.async_create_task(connect())
    async_dispatcher_connect(hass, SIGNAL_EVENT, event_callback)

    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    return True


class RfplayerDevice(Entity):
    """Representation of a Rfplayer device.

    Contains the common logic for Rfplayer entities.
    """

    platform = None
    _state = None
    _available = True

    def __init__(
        self,
        device_id,
        initial_event=None,
        name=None,
        aliases=None,
        group=True,
        group_aliases=None,
        nogroup_aliases=None,
        fire_event=False,
        signal_repetitions=DEFAULT_SIGNAL_REPETITIONS,
    ):
        """Initialize the device."""
        # Rflink specific attributes for every component type
        self._initial_event = initial_event
        self._device_id = device_id
        if name:
            self._name = name
        else:
            self._name = device_id

        self._aliases = aliases
        self._group = group
        self._group_aliases = group_aliases
        self._nogroup_aliases = nogroup_aliases
        self._should_fire_event = fire_event
        self._signal_repetitions = signal_repetitions

    @callback
    def handle_event_callback(self, event):
        """Handle incoming event for device type."""
        # Call platform specific event handler
        self._handle_event(event)

        # Propagate changes through ha
        self.async_write_ha_state()

        # Put command onto bus for user to subscribe to
        if self._should_fire_event and identify_event_type(event) == EVENT_KEY_COMMAND:
            self.hass.bus.async_fire(
                EVENT_BUTTON_PRESSED,
                {ATTR_ENTITY_ID: self.entity_id, ATTR_STATE: event[EVENT_KEY_COMMAND]},
            )
            _LOGGER.debug(
                "Fired bus event for %s: %s", self.entity_id, event[EVENT_KEY_COMMAND]
            )

    def _handle_event(self, event):
        """Platform specific event handler."""
        raise NotImplementedError()

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def name(self):
        """Return a name for the device."""
        return self._name

    @property
    def is_on(self):
        """Return true if device is on."""
        if self.assumed_state:
            return False
        return self._state

    @property
    def assumed_state(self):
        """Assume device state until first device event sets state."""
        return self._state is None

    @property
    def available(self):
        """Return True if entity is available."""
        return self._available

    @callback
    def _availability_callback(self, availability):
        """Update availability state."""
        self._available = availability
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        """Register update callback."""
        await super().async_added_to_hass()
        # Remove temporary bogus entity_id if added
        tmp_entity = TMP_ENTITY.format(self._device_id)
        if (
            tmp_entity
            in self.hass.data[DATA_ENTITY_LOOKUP][EVENT_KEY_COMMAND][self._device_id]
        ):
            self.hass.data[DATA_ENTITY_LOOKUP][EVENT_KEY_COMMAND][
                self._device_id
            ].remove(tmp_entity)

        # Register id and aliases
        self.hass.data[DATA_ENTITY_LOOKUP][EVENT_KEY_COMMAND][self._device_id].append(
            self.entity_id
        )
        if self._group:
            self.hass.data[DATA_ENTITY_GROUP_LOOKUP][EVENT_KEY_COMMAND][
                self._device_id
            ].append(self.entity_id)
        # aliases respond to both normal and group commands (allon/alloff)
        if self._aliases:
            for _id in self._aliases:
                self.hass.data[DATA_ENTITY_LOOKUP][EVENT_KEY_COMMAND][_id].append(
                    self.entity_id
                )
                self.hass.data[DATA_ENTITY_GROUP_LOOKUP][EVENT_KEY_COMMAND][_id].append(
                    self.entity_id
                )
        # group_aliases only respond to group commands (allon/alloff)
        if self._group_aliases:
            for _id in self._group_aliases:
                self.hass.data[DATA_ENTITY_GROUP_LOOKUP][EVENT_KEY_COMMAND][_id].append(
                    self.entity_id
                )
        # nogroup_aliases only respond to normal commands
        if self._nogroup_aliases:
            for _id in self._nogroup_aliases:
                self.hass.data[DATA_ENTITY_LOOKUP][EVENT_KEY_COMMAND][_id].append(
                    self.entity_id
                )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_AVAILABILITY, self._availability_callback
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_HANDLE_EVENT.format(self.entity_id),
                self.handle_event_callback,
            )
        )

        # Process the initial event now that the entity is created
        if self._initial_event:
            self.handle_event_callback(self._initial_event)


class RfplayerCommand(RfplayerDevice):
    """Singleton class to make Rflink command interface available to entities.

    This class is to be inherited by every Entity class that is actionable
    (switches/lights). It exposes the Rflink command interface for these
    entities.

    The Rflink interface is managed as a class level and set during setup (and
    reset on reconnect).
    """

    # Keep repetition tasks to cancel if state is changed before repetitions
    # are sent
    _repetition_task = None

    _protocol = None

    @classmethod
    def set_rfplayer_protocol(cls, protocol, wait_ack=None):
        """Set the Rflink asyncio protocol as a class variable."""
        cls._protocol = protocol
        if wait_ack is not None:
            cls._wait_ack = wait_ack

    @classmethod
    def is_connected(cls):
        """Return connection status."""
        return bool(cls._protocol)

    @classmethod
    async def send_command(cls, device_id, action):
        """Send device command to Rflink and wait for acknowledgement."""
        return await cls._protocol.send_command_ack(device_id, action)

    async def _async_handle_command(self, command, *args):
        """Do bookkeeping for command, send it to rflink and update state."""
        self.cancel_queued_send_commands()

        if command == "turn_on":
            cmd = "on"
            self._state = True

        elif command == "turn_off":
            cmd = "off"
            self._state = False

        elif command == "dim":
            # convert brightness to rflink dim level
            # cmd = str(brightness_to_rfplayer(args[0])) TODO
            self._state = True

        elif command == "toggle":
            cmd = "on"
            # if the state is unknown or false, it gets set as true
            # if the state is true, it gets set as false
            self._state = self._state in [None, False]

        # Cover options for RFlink
        elif command == "close_cover":
            cmd = "DOWN"
            self._state = False

        elif command == "open_cover":
            cmd = "UP"
            self._state = True

        elif command == "stop_cover":
            cmd = "STOP"
            self._state = True

        # Send initial command and queue repetitions.
        # This allows the entity state to be updated quickly and not having to
        # wait for all repetitions to be sent
        await self._async_send_command(cmd, self._signal_repetitions)

        # Update state of entity
        self.async_write_ha_state()

    def cancel_queued_send_commands(self):
        """Cancel queued signal repetition commands.

        For example when user changed state while repetitions are still
        queued for broadcast. Or when an incoming Rflink command (remote
        switch) changes the state.
        """
        # cancel any outstanding tasks from the previous state change
        if self._repetition_task:
            self._repetition_task.cancel()

    async def _async_send_command(self, cmd, repetitions):
        """Send a command for device to Rflink gateway."""
        _LOGGER.debug("Sending command: %s to Rflink device: %s", cmd, self._device_id)

        if not self.is_connected():
            raise HomeAssistantError("Cannot send command, not connected!")

        if self._wait_ack:
            # Puts command on outgoing buffer then waits for Rflink to confirm
            # the command has been sent out.
            await self._protocol.send_command_ack(self._device_id, cmd)
        else:
            # Puts command on outgoing buffer and returns straight away.
            # Rflink protocol/transport handles asynchronous writing of buffer
            # to serial/tcp device. Does not wait for command send
            # confirmation.
            self._protocol.send_command(self._device_id, cmd)

        if repetitions > 1:
            self._repetition_task = self.hass.async_create_task(
                self._async_send_command(cmd, repetitions - 1)
            )


class SwitchableRfplayerDevice(RfplayerCommand, RestoreEntity):
    """Rflink entity which can switch on/off (eg: light, switch)."""

    async def async_added_to_hass(self):
        """Restore RFLink device state (ON/OFF)."""
        await super().async_added_to_hass()
        if (old_state := await self.async_get_last_state()) is not None:
            self._state = old_state.state == STATE_ON

    def _handle_event(self, event):
        """Adjust state if Rflink picks up a remote command for this device."""
        self.cancel_queued_send_commands()

        command = event["command"]
        if command in ["on", "allon"]:
            self._state = True
        elif command in ["off", "alloff"]:
            self._state = False

    async def async_turn_on(self, **kwargs):
        """Turn the device on."""
        await self._async_handle_command("turn_on")

    async def async_turn_off(self, **kwargs):
        """Turn the device off."""
        await self._async_handle_command("turn_off")
