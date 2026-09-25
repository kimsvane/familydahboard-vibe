"""Config flow for Family Dashboard."""

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import (
    DOMAIN,
    FamilyDashboardApiError,
    FamilyDashboardAuthError,
    async_fetch_payloads,
    normalize_dashboard_url,
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Optional(CONF_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class FamilyDashboardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Family Dashboard config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial connection step."""
        if user_input is not None:
            token = str(user_input.get(CONF_TOKEN) or "").strip()
            try:
                base_url = normalize_dashboard_url(user_input[CONF_URL])
                await async_fetch_payloads(self.hass, base_url, token or None)
            except ValueError:
                errors = {"base": "invalid_url"}
            except FamilyDashboardAuthError:
                errors = {"base": "invalid_auth"}
            except aiohttp.ClientResponseError as err:
                errors = {
                    "base": "invalid_response"
                    if err.status >= 400
                    else "cannot_connect"
                }
            except (TimeoutError, aiohttp.ClientError):
                errors = {"base": "cannot_connect"}
            except FamilyDashboardApiError:
                errors = {"base": "invalid_response"}
            else:
                data = {CONF_URL: base_url}
                if token:
                    data[CONF_TOKEN] = token
                return self.async_create_entry(title="Family Dashboard", data=data)

            return self.async_show_form(
                step_id="user",
                data_schema=self.add_suggested_values_to_schema(
                    STEP_USER_DATA_SCHEMA, user_input
                ),
                errors=errors,
            )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle authentication renewal for an existing entry."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate and save replacement credentials."""
        entry = self._get_reauth_entry()
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=STEP_REAUTH_DATA_SCHEMA
            )

        token = str(user_input.get(CONF_TOKEN) or "").strip()
        try:
            await async_fetch_payloads(
                self.hass, entry.data[CONF_URL], token or None
            )
        except FamilyDashboardAuthError:
            errors = {"base": "invalid_auth"}
        except aiohttp.ClientResponseError as err:
            errors = {
                "base": "invalid_response"
                if err.status >= 400
                else "cannot_connect"
            }
        except (TimeoutError, aiohttp.ClientError):
            errors = {"base": "cannot_connect"}
        except FamilyDashboardApiError:
            errors = {"base": "invalid_response"}
        else:
            return self.async_update_reload_and_abort(
                entry,
                data_updates={CONF_TOKEN: token or None},
            )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_REAUTH_DATA_SCHEMA, user_input
            ),
            errors=errors,
        )
