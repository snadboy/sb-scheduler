"""Config flow for the Scheduler component."""
import secrets

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
)

from . import const


class SchedulerConfigFlow(config_entries.ConfigFlow, domain=const.DOMAIN):
    """Config flow for Scheduler."""

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""

        # Only a single instance of the integration
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        id = secrets.token_hex(6)

        await self.async_set_unique_id(id)
        self._abort_if_unique_id_configured(updates=user_input)

        return self.async_create_entry(title="Scheduler", data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return SchedulerOptionsFlow()


class SchedulerOptionsFlow(config_entries.OptionsFlow):
    """Options for Scheduler.

    Upstream has no options flow at all: the workday sensor is a hard-coded
    entity id and failed actions are silent. Both are configurable here.
    """

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        const.CONF_WORKDAY_ENTITY,
                        default=options.get(
                            const.CONF_WORKDAY_ENTITY, const.DEFAULT_WORKDAY_ENTITY
                        ),
                    ): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
                    vol.Required(
                        const.CONF_NOTIFY_ON_FAILURE,
                        default=options.get(
                            const.CONF_NOTIFY_ON_FAILURE, const.DEFAULT_NOTIFY_ON_FAILURE
                        ),
                    ): BooleanSelector(),
                }
            ),
        )
