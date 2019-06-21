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

import time
from datetime import datetime, timedelta
import logging
from .actor import PoupoolModel
from .actor import PoupoolActor
from .actor import repeat, do_repeat
from .util import constrain, Timer
from .config import config

logger = logging.getLogger(__name__)


class PWM(PoupoolActor):

    SECURITY_DURATION = int(config["disinfection", "security_duration"])

    def __init__(self, name, pump, period=120, min_runtime=3):
        super().__init__()
        self.__name = name
        self.__pump = pump
        self.period = period
        self.__last = None
        self.__duration = 0
        self.__state = False
        self.__security_duration = Timer("PWM for %s" % name)
        self.__security_duration.delay = timedelta(hours=PWM.SECURITY_DURATION)
        self.__security_reset = datetime.now() + timedelta(days=1)
        self.__min_runtime = min_runtime
        self.value = 0.0

    def do_cancel(self):
        # Clear the security duration counter and last time during a pause
        self.__security_duration.clear()
        self.__last = None
        super().do_cancel()

    @repeat(delay=1)
    def do_run(self):
        now = time.time()
        if self.__last is not None:
            diff = now - self.__last
            self.__duration += diff
            self.__duration = constrain(self.__duration, 0, self.period)
            duty_on = self.value * self.period
            # Avoid short commutations. If below min_runtime but higher than zero we run at
            # min_runtime. If higher than period - min_runtime, we run at period (max)
            if duty_on != 0 and duty_on < self.__min_runtime:
                duty_on = self.__min_runtime
            elif duty_on > self.period - self.__min_runtime:
                duty_on = self.period
            duty_off = self.period - duty_on
            if int(now) % 10 == 0:
                logger.debug("%s duty (on/off): %.1f/%.1f state: %d duration: %.1f" %
                             (self.__name, duty_on, duty_off, self.__state, self.__duration))
            if self.__state:
                self.__security_duration.update(datetime.now())
                if self.__duration >= duty_on and duty_on != self.period:
                    self.__duration = 0
                    self.__state = False
                    self.__pump.off()
            else:
                self.__security_duration.update(datetime.now(), 0)
                security_ok = not self.__security_duration.elapsed()
                if self.__duration >= duty_off and duty_off != self.period and security_ok:
                    self.__duration = 0
                    self.__state = True
                    self.__pump.on()
        if datetime.now() > self.__security_reset:
            self.__security_duration.reset()
            self.__security_reset += timedelta(days=1)
        self.__last = now


class PController(object):

    def __init__(self, pterm=0.1, scale=1.0):
        self.setpoint = 0
        self.current = 0
        self.pterm = pterm
        self.__scale = scale

    def compute(self):
        error = self.setpoint - self.current
        return constrain((self.pterm * self.__scale) * error, 0, 1)


class Disinfection(PoupoolActor):

    STATE_REFRESH_DELAY = 10
    START_DELAY = int(config["disinfection", "start_delay"])
    WAITING_DELAY = int(config["disinfection", "waiting_delay"])
    PH_PWM_PERIOD = int(config["disinfection", "ph_pwm_period"])
    CL_PWM_PERIOD = int(config["disinfection", "cl_pwm_period"])
    CL_PWM_PERIOD_CONSTANT = int(config["disinfection", "cl_pwm_period_constant"])

    curves = {
        "low": lambda x: -45.162 * x + 1002,         # 0.8
        "mid": lambda x: -50 * x + 1065,             # 1.0
        "mid_high": lambda x: -55.691 * x + 1138.9,  # 1.3
        "high": lambda x: -58.618 * x + 1178.1,      # 1.5
    }

    states = [
        "halt",
        "waiting",
        "constant",
        {"name": "running", "initial": "adjusting", "children": [
            "adjusting",
            "treating"]}]

    def __init__(self, encoder, devices, sensors_reader, sensors_writer, disable=False):
        super().__init__()
        self.__is_disabled = disable
        self.__encoder = encoder
        self.__devices = devices
        self.__sensors_reader = sensors_reader
        self.__sensors_writer = sensors_writer
        # pH
        self.__ph_enable = True
        self.__ph = PWM.start("pH", self.__devices.get_pump("ph")).proxy()
        self.__ph.period = Disinfection.PH_PWM_PERIOD
        self.__ph_controller = PController(pterm=-1.0)
        self.__ph_controller.setpoint = 7
        # ORP
        self.__orp_enable = True
        self.__orp_controller = PController(pterm=1.0, scale=0.005)
        self.__orp_controller.setpoint = 700
        # Chlorine
        self.__cl = PWM.start("cl", self.__devices.get_pump("cl")).proxy()
        self.__cl.period = Disinfection.CL_PWM_PERIOD
        self.__cl_constant = 0.5
        self.__free_chlorine = "low"
        # Initialize the state machine
        self.__machine = PoupoolModel(model=self, states=Disinfection.states, initial="halt")

        self.__machine.add_transition("run", "halt", "waiting", unless="is_disabled")
        self.__machine.add_transition("run", "waiting", "running")
        self.__machine.add_transition("halt", ["constant", "waiting", "running"], "halt")
        self.__machine.add_transition("constant", ["halt", "waiting", "running"], "constant")
        self.__machine.add_transition("adjust", "running_treating", "running_adjusting")
        self.__machine.add_transition("treat", "running_adjusting", "running_treating")

    def ph_enable(self, value):
        self.__ph_enable = value
        logger.info("pH adjustment is %sabled" % ("en" if value else "dis"))

    def orp_enable(self, value):
        self.__orp_enable = value
        logger.info("ORP adjustment is %sabled" % ("en" if value else "dis"))

    def ph_setpoint(self, value):
        self.__ph_controller.setpoint = value
        logger.info("pH setpoint set to: %f" % self.__ph_controller.setpoint)

    def cl_constant(self, value):
        self.__cl_constant = value
        logger.info("Chlore constant value set to: %f" % self.__cl_constant)

    def free_chlorine(self, value):
        if value in self.curves:
            self.__free_chlorine = value
            logger.info("Free chlorine level set to: %s" % self.__free_chlorine)
        else:
            logger.error("Unsupported free chlorine level: %s" % value)

    def ph_pterm(self, value):
        # We assume here that we use "pH minus" chemicals, therefore inverse the term.
        self.__ph_controller.pterm = -value
        logger.info("pH pterm set to: %f" % self.__ph_controller.pterm)

    def orp_pterm(self, value):
        self.__orp_controller.pterm = value
        logger.info("ORP pterm set to: %f" % self.__orp_controller.pterm)

    def is_disabled(self):
        return self.__is_disabled

    def on_enter_halt(self):
        logger.info("Entering halt state")
        self.__encoder.disinfection_state("halt")
        self.__ph.value = 0
        self.__cl.value = 0
        self.__ph.do_cancel()
        self.__cl.do_cancel()
        self.__devices.get_pump("ph").off()
        self.__devices.get_pump("cl").off()
        self.__encoder.disinfection_cl_feedback(0)
        self.__encoder.disinfection_ph_feedback(0)
        self.__sensors_writer.do_cancel()

    def on_enter_waiting(self):
        logger.info("Entering waiting state")
        self.__encoder.disinfection_state("waiting")
        self._proxy.do_delay(Disinfection.START_DELAY, "run")

    @do_repeat()
    def on_enter_constant(self):
        logger.info("Entering constant state")
        self.__encoder.disinfection_state("constant")
        self.__cl.period = Disinfection.CL_PWM_PERIOD_CONSTANT
        self.__cl.do_run()

    @repeat(delay=10)
    def do_repeat_constant(self):
        cl_feedback = self.__cl_constant / 100. if self.__orp_enable else 0
        self.__encoder.disinfection_cl_feedback(int(round(100 * cl_feedback)))
        self.__cl.value = cl_feedback

    def on_enter_running(self):
        logger.info("Entering running state")
        self.__ph.do_run()
        self.__cl.period = Disinfection.CL_PWM_PERIOD
        self.__cl.do_run()
        self.__sensors_writer.do_write()

    def on_enter_running_adjusting(self):
        logger.debug("Entering adjusting state")
        self.__encoder.disinfection_state("adjusting")
        # pH
        ph = self.__sensors_reader.get_ph().get()
        self.__ph_controller.current = ph
        ph_feedback = self.__ph_controller.compute() if self.__ph_enable else 0
        self.__encoder.disinfection_ph_feedback(int(round(ph_feedback * 100)))
        logger.debug("pH: %.2f feedback: %.2f" % (ph, ph_feedback))
        self.__ph.value = ph_feedback
        # ORP/Chlorine
        orp = self.__sensors_reader.get_orp().get()
        orp_setpoint = self.curves[self.__free_chlorine](ph)
        # Round to +/- 5 to avoid too many step changes
        orp_setpoint = 5 * round(orp_setpoint / 5)
        self.__orp_controller.setpoint = orp_setpoint
        self.__orp_controller.current = orp
        cl_feedback = self.__orp_controller.compute() if self.__orp_enable else 0
        self.__encoder.disinfection_cl_feedback(int(round(cl_feedback * 100)))
        self.__encoder.disinfection_orp_setpoint(int(orp_setpoint))
        logger.debug("ORP: %d setpoint: %d feedback: %.2f" % (orp, orp_setpoint, cl_feedback))
        self.__cl.value = cl_feedback
        self._proxy.treat.defer()

    def on_enter_running_treating(self):
        logger.debug("Entering treating state")
        self.__encoder.disinfection_state("treating")
        self._proxy.do_delay(Disinfection.WAITING_DELAY, "adjust")
