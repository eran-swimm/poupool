# Poupool - swimming pool control software
# Copyright (C) 2019 Cyril Jaquier
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import pykka
import transitions
#from transitions.extensions import HierarchicalGraphMachine as Machine
from transitions.extensions import HierarchicalMachine as Machine
import time
from datetime import datetime
import logging
import functools
import re
from threading import Timer

logger = logging.getLogger(__name__)


class StopRepeatException(Exception):
    pass


def repeat(delay=10):
    assert delay >= 0

    def wrap(func):
        @functools.wraps(func)
        def wrapped_func(self, *args, **kwargs):
            try:
                func(self, *args, **kwargs)
            except StopRepeatException:
                pass
            else:
                if delay > 0:
                    self._proxy.do_delay(delay, func.__name__)
                else:
                    function = getattr(self._proxy, func.__name__)
                    function(*args, **kwargs)
        return wrapped_func
    return wrap


def do_repeat():
    def wrap(func):
        @functools.wraps(func)
        def wrapped_func(self, *args, **kwargs):
            try:
                func(self, *args, **kwargs)
            except StopRepeatException:
                pass
            else:
                method = re.sub("on_enter_", "do_repeat_", func.__name__)
                function = getattr(self, method)
                function()
        return wrapped_func
    return wrap


class PoupoolModel(Machine):

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("before_state_change", []).extend(["do_cancel", self.__update_state_time])
        super().__init__(
            auto_transitions=False,
            ignore_invalid_triggers=True,
            *args,
            **kwargs
        )
        self.__state_time = None

    def __update_state_time(self):
        self.__state_time = datetime.now()

    def get_time_in_state(self):
        return datetime.now() - self.__state_time


class PoupoolActor(pykka.ThreadingActor):

    def __init__(self):
        super().__init__()
        self._proxy = self.actor_ref.proxy()
        self.__timer = None

    def on_failure(self, exception_type, exception_value, traceback):
        # The actor is going to die
        logger.fatal(exception_type, exception_value, traceback)

    def on_stop(self):
        self.do_cancel()

    def get_actor(self, name):
        fsm = pykka.ActorRegistry.get_by_class_name(name)
        if fsm:
            return fsm[0].proxy()
        logger.critical("Actor %s not found!!!" % name)
        return None

    def do_cancel(self):
        if self.__timer:
            self.__timer.cancel()
            self.__timer = None

    def do_delay(self, delay, method, *args, **kwargs):
        assert type(method) == str
        # Stop an already running timer
        self.do_cancel()
        func = getattr(self._proxy, method)
        self.__timer = Timer(delay, func.defer, *args, **kwargs)
        self.__timer.start()
