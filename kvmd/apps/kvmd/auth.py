# ========================================================================== #
#                                                                            #
#    KVMD - The main PiKVM daemon.                                           #
#                                                                            #
#    Copyright (C) 2018-2024  Maxim Devaev <mdevaev@gmail.com>               #
#                                                                            #
#    This program is free software: you can redistribute it and/or modify    #
#    it under the terms of the GNU General Public License as published by    #
#    the Free Software Foundation, either version 3 of the License, or       #
#    (at your option) any later version.                                     #
#                                                                            #
#    This program is distributed in the hope that it will be useful,         #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#    GNU General Public License for more details.                            #
#                                                                            #
#    You should have received a copy of the GNU General Public License       #
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.  #
#                                                                            #
# ========================================================================== #


import dataclasses
import time

import secrets
import pyotp

from ...logging import get_logger

from ... import aiotools

from ...plugins.auth import BaseAuthService
from ...plugins.auth import get_auth_service_class

from ...htserver import HttpExposed


# =====
@dataclasses.dataclass(frozen=True)
class _Session:
    user:      str
    expire_ts: int

    def __post_init__(self) -> None:
        assert self.user.strip()
        assert self.user
        assert self.expire_ts >= 0


class AuthManager:
    def __init__(
        self,
        enabled: bool,
        unauth_paths: list[str],

        internal_type: str,
        internal_kwargs: dict,
        force_internal_users: list[str],

        external_type: str,
        external_kwargs: dict,

        totp_secret_path: str,
    ) -> None:

        self.__enabled = enabled
        if not enabled:
            get_logger().warning("AUTHORIZATION IS DISABLED")

        self.__unauth_paths = frozenset(unauth_paths)  # To speed up
        for path in self.__unauth_paths:
            get_logger().warning("Authorization is disabled for API %r", path)

        self.__internal_service: (BaseAuthService | None) = None
        if enabled:
            self.__internal_service = get_auth_service_class(internal_type)(**internal_kwargs)
            get_logger().info("Using internal auth service %r", self.__internal_service.get_plugin_name())

        self.__force_internal_users = force_internal_users

        self.__external_service: (BaseAuthService | None) = None
        if enabled and external_type:
            self.__external_service = get_auth_service_class(external_type)(**external_kwargs)
            get_logger().info("Using external auth service %r", self.__external_service.get_plugin_name())

        self.__totp_secret_path = totp_secret_path

        self.__sessions: dict[str, _Session] = {}  # {token: session}

    def is_auth_enabled(self) -> bool:
        return self.__enabled

    def is_auth_required(self, exposed: HttpExposed) -> bool:
        return (
            self.is_auth_enabled()
            and exposed.auth_required
            and exposed.path not in self.__unauth_paths
        )

    async def authorize(self, user: str, passwd: str) -> bool:
        assert user == user.strip()
        assert user
        assert self.__enabled
        assert self.__internal_service

        if self.__totp_secret_path:
            with open(self.__totp_secret_path) as file:
                secret = file.read().strip()
            if secret:
                code = passwd[-6:]
                if not pyotp.TOTP(secret).verify(code, valid_window=1):
                    get_logger().error("Got access denied for user %r by TOTP", user)
                    return False
                passwd = passwd[:-6]

        if user not in self.__force_internal_users and self.__external_service:
            service = self.__external_service
        else:
            service = self.__internal_service

        ok = (await service.authorize(user, passwd))
        pname = service.get_plugin_name()
        if ok:
            get_logger().info("Authorized user %r via auth service %r", user, pname)
        else:
            get_logger().error("Got access denied for user %r from auth service %r", user, pname)
        return ok

    async def login(self, user: str, passwd: str, expire: int) -> (str | None):
        assert user == user.strip()
        assert user
        assert expire >= 0
        assert self.__enabled
        if (await self.authorize(user, passwd)):
            token = self.__make_new_token()
            session = _Session(
                user=user,
                expire_ts=(0 if expire <= 0 else (self.__get_now_ts() + expire)),
            )
            self.__sessions[token] = session
            get_logger().info("Logged in user %r (expire_ts=%d)", session.user, session.expire_ts)
            return token
        else:
            return None

    def __make_new_token(self) -> str:
        for _ in range(10):
            token = secrets.token_hex(32)
            if token not in self.__sessions:
                return token
        raise AssertionError("Can't generate new unique token")

    def __get_now_ts(self) -> int:
        return int(time.monotonic())

    def logout(self, token: str) -> None:
        assert self.__enabled
        if token in self.__sessions:
            user = self.__sessions[token].user
            count = 0
            for (key_t, session) in list(self.__sessions.items()):
                if session.user == user:
                    count += 1
                    del self.__sessions[key_t]
            get_logger().info("Logged out user %r (was=%d)", user, count)

    def check(self, token: str) -> (str | None):
        assert self.__enabled
        session = self.__sessions.get(token)
        if session is not None:
            if session.expire_ts <= 0:
                # Infinite session
                assert session.user
                return session.user
            else:
                # Limited session
                if self.__get_now_ts() < session.expire_ts:
                    assert session.user
                    return session.user
                else:
                    del self.__sessions[token]
        return None

    @aiotools.atomic_fg
    async def cleanup(self) -> None:
        if self.__enabled:
            assert self.__internal_service
            await self.__internal_service.cleanup()
            if self.__external_service:
                await self.__external_service.cleanup()
