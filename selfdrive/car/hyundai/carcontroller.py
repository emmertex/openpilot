from selfdrive.car import limit_steer_rate
from selfdrive.boardd.boardd import can_list_to_can_capnp
from selfdrive.car.hyundai.hyundaican import create_lkas11, \
                                             create_clu11, create_mdps12, \
                                             learn_checksum, create_spas11, create_spas12
from selfdrive.car.hyundai.values import Buttons, CAR, FEATURES
from selfdrive.can.packer import CANPacker
from selfdrive.car.modules.ALCA_module import ALCAController
import numpy as np
import zmq
import math
from selfdrive.services import service_list
import selfdrive.messaging as messaging
from common.params import Params
from selfdrive.config import Conversions as CV

# Steer torque limits

class SteerLimitParams:
  STEER_MAX = 255   # >255 results in frozen torque, >409 results in no torque
  STEER_DELTA_UP = 3
  STEER_DELTA_DOWN = 5
  STEER_ANG_MAX = 20          # SPAS Max Angle
  STEER_ANG_MAX_RATE = 0.4    # SPAS Degrees per ms
  DIVIDER = 2.0     # Must be > 1.0

class CarController(object):

  def __init__(self, dbc_name, car_fingerprint, enable_camera):
    self.apply_steer_last = 0
    self.turning_signal_timer = 0
    self.car_fingerprint = car_fingerprint
    self.lkas11_cnt = 0
    self.mdps12_cnt = 0
    self.cnt = 0
    self.last_resume_cnt = 0
    self.map_speed = 0
    self.enable_camera = enable_camera
    # True when camera present, and we need to replace all the camera messages
    # otherwise we forward the camera msgs and we just replace the lkas cmd signals
    self.camera_disconnected = False
    self.packer = CANPacker(dbc_name)
    context = zmq.Context()
    self.params = Params()
    self.map_data_sock = messaging.sub_sock(context, service_list['liveMapData'].port, conflate=True)
    self.speed_conv = 3.6
    self.speed_adjusted = False
    self.checksum = "NONE"
    self.checksum_learn_cnt = 0
    self.en_cnt = 0
    self.apply_steer_ang = 0.0
    self.en_spas = 3
    self.mdps11_stat_last = 0
    self.lkas = False
    self.spas_present = True # TODO Make Automatic

    self.ALCA = ALCAController(self,True,False)  # Enabled True and SteerByAngle only False


  def update(self, sendcan, enabled, CS, actuators, pcm_cancel_cmd, hud_alert):

    if not self.enable_camera:
      return

    if CS.camcan > 0:
      if self.checksum == "NONE":
        self.checksum = learn_checksum(self.packer, CS.lkas11)
        print ("Discovered Checksum", self.checksum)
        if self.checksum == "NONE":
          return
    elif CS.steer_error == 1:
      if self.checksum_learn_cnt > 200:
        self.checksum_learn_cnt = 0
        if self.checksum == "NONE":
          print ("Testing 6B Checksum")
          self.checksum == "6B"
        elif self.checksum == "6B":
          print ("Testing 7B Checksum")
          self.checksum == "7B"
        elif self.checksum == "7B":
          print ("Testing CRC8 Checksum")
          self.checksum == "crc8"
        else:
          self.checksum == "NONE"
      else:
        self.checksum_learn_cnt += 1

    force_enable = False

    # I don't care about your opinion, deal with it!
    if (CS.cstm_btns.get_button_status("alwon") > 0) and CS.acc_active:
      enabled = True
      force_enable = True

    if (self.car_fingerprint in FEATURES["soft_disable"] and CS.v_wheel < 16.8):
      enabled = False
      force_enable = False


    if (CS.left_blinker_on == 1 or CS.right_blinker_on == 1):
      self.turning_signal_timer = 100  # Disable for 1.0 Seconds after blinker turned off

    #update custom UI buttons and alerts
    CS.UE.update_custom_ui()
    if (self.cnt % 100 == 0):
      CS.cstm_btns.send_button_info()
      CS.UE.uiSetCarEvent(CS.cstm_btns.car_folder,CS.cstm_btns.car_name)

    # Get the angle from ALCA.
    alca_enabled = False
    alca_steer = 0.
    alca_angle = 0.
    turn_signal_needed = 0
    # Update ALCA status and custom button every 0.1 sec.
    if self.ALCA.pid == None:
      self.ALCA.set_pid(CS)
    self.ALCA.update_status(CS.cstm_btns.get_button_status("alca") > 0)

    alca_angle, alca_steer, alca_enabled, turn_signal_needed = self.ALCA.update(enabled, CS, self.cnt, actuators)
    if force_enable and not CS.acc_active:
      apply_steer = int(round(actuators.steer * SteerLimitParams.STEER_MAX))
    else:
      apply_steer = int(round(alca_steer * SteerLimitParams.STEER_MAX))

    # SPAS limit angle extremes for safety
    apply_steer_ang_req = np.clip(actuators.steerAngle, -1*(SteerLimitParams.STEER_ANG_MAX), SteerLimitParams.STEER_ANG_MAX)
    # SPAS limit angle rate for safety
    if abs(self.apply_steer_ang - apply_steer_ang_req) > 0.6:
      if apply_steer_ang_req > self.apply_steer_ang:
        self.apply_steer_ang += 0.5
      else:
        self.apply_steer_ang -= 0.5
    else:
      self.apply_steer_ang = apply_steer_ang_req

    # Limit steer rate for safety
    apply_steer = limit_steer_rate(apply_steer, self.apply_steer_last, SteerLimitParams, CS.steer_torque_driver)

    if alca_enabled:
      self.turning_signal_timer = 0

    if self.turning_signal_timer > 0:
      self.turning_signal_timer = self.turning_signal_timer - 1
      turning_signal = 1
    else:
      turning_signal = 0

    # Use LKAS or SPAS
    if CS.mdps11_stat == 7 or CS.v_wheel > 2.7:
      self.lkas = True
    elif CS.v_wheel < 0.1:
      self.lkas = False
    if self.spas_present:
      self.lkas = True

    # If ALCA is disabled, and turning indicators are turned on, we do not want OP to steer,
    if not enabled or (turning_signal and not alca_enabled):
      if self.lkas:
        apply_steer = 0
      else:
        self.apply_steer_ang = 0.0
        self.en_cnt = 0

    steer_req = 1 if enabled and self.lkas else 0

    self.apply_steer_last = apply_steer

    can_sends = []

    self.lkas11_cnt = self.cnt % 0x10
    self.clu11_cnt = self.cnt % 0x10
    self.mdps12_cnt = self.cnt % 0x100
    self.spas_cnt = self.cnt % 0x200

    can_sends.append(create_lkas11(self.packer, self.car_fingerprint, apply_steer, steer_req, self.lkas11_cnt, \
                                  enabled if self.lkas else False, False if CS.camcan == 0 else CS.lkas11, hud_alert, (CS.cstm_btns.get_button_status("cam") > 0), \
                                  (False if CS.camcan == 0 else True), self.checksum))

    if CS.camcan > 0:
      can_sends.append(create_mdps12(self.packer, self.car_fingerprint, self.mdps12_cnt, CS.mdps12, CS.lkas11, \
                                    CS.camcan, self.checksum))

    # SPAS11 50hz
    if (self.cnt % 2) == 0 and not self.spas_present:
      if CS.mdps11_stat == 7 and not self.mdps11_stat_last == 7:
        self.en_spas == 7
        self.en_cnt = 0

      if self.en_spas == 7 and self.en_cnt >= 8:
        self.en_spas = 3
        self.en_cnt = 0

      if self.en_cnt < 8 and enabled and not self.lkas:
        self.en_spas = 4
      elif self.en_cnt >= 8 and enabled and not self.lkas:
        self.en_spas = 5
      
      if self.lkas or not enabled:
        self.apply_steer_ang = CS.mdps11_strang
        self.en_spas = 3
        self.en_cnt = 0

      self.mdps11_stat_last = CS.mdps11_stat
      self.en_cnt += 1
      can_sends.append(create_spas11(self.packer, (self.spas_cnt / 2), self.en_spas, self.apply_steer_ang, self.checksum))
    
    # SPAS12 20Hz
    if (self.cnt % 5) == 0 and not self.spas_present:
      can_sends.append(create_spas12(self.packer))

    # Force Disable
    if pcm_cancel_cmd and (not force_enable):
      can_sends.append(create_clu11(self.packer, CS.clu11, Buttons.CANCEL, 0))
    elif CS.stopped and (self.cnt - self.last_resume_cnt) > 5:
      self.last_resume_cnt = self.cnt
      can_sends.append(create_clu11(self.packer, CS.clu11, Buttons.RES_ACCEL, 0))


    # Speed Limit Related Stuff  Lot's of comments for others to understand!
    # Run this twice a second
    if (self.cnt % 50) == 0:
      if self.params.get("LimitSetSpeed") == "1" and self.params.get("SpeedLimitOffset") is not None:
        # If Not Enabled, or cruise not set, allow auto speed adjustment again
        if not (enabled and CS.acc_active_real):
          self.speed_adjusted = False
        # Attempt to read the speed limit from zmq
        map_data = messaging.recv_one_or_none(self.map_data_sock)
        # If we got a message
        if map_data != None:
          # See if we use Metric or dead kings extremeties for measurements, and set a variable to the conversion value
          if bool(self.params.get("IsMetric")):
            self.speed_conv = CV.MS_TO_KPH
          else:
            self.speed_conv = CV.MS_TO_MPH

          # If the speed limit is valid
          if map_data.liveMapData.speedLimitValid:
            last_speed = self.map_speed
            # Get the speed limit, and add the offset to it,
            v_speed = (map_data.liveMapData.speedLimit + float(self.params.get("SpeedLimitOffset")))
            ## Stolen curvature code from planner.py, and updated it for us
            v_curvature = 45.0
            if map_data.liveMapData.curvatureValid:
              v_curvature = math.sqrt(1.85 / max(1e-4, abs(map_data.liveMapData.curvature)))
            # Use the minimum between Speed Limit and Curve Limit, and convert it as needed
            #self.map_speed = min(v_speed, v_curvature) * self.speed_conv
            self.map_speed = v_speed * self.speed_conv
            # Compare it to the last time the speed was read.  If it is different, set the flag to allow it to auto set our speed
            if last_speed != self.map_speed:
              self.speed_adjusted = False
          else:
            # If it is not valid, set the flag so the cruise speed won't be changed.
            self.map_speed = 0
            self.speed_adjusted = True
      else:
        self.speed_adjusted = True


    # Ensure we have cruise IN CONTROL, so we don't do anything dangerous, like turn cruise on
    # Ensure the speed limit is within range of the stock cruise control capabilities
    # Do the spamming 10 times a second, we might get from 0 to 10 successful
    # Only do this if we have not yet set the cruise speed
    if CS.acc_active_real and not self.speed_adjusted and self.map_speed > (8.5 * self.speed_conv) and (self.cnt % 9 == 0 or self.cnt % 9 == 1):
      # Use some tolerance because of Floats being what they are...
      if (CS.cruise_set_speed * self.speed_conv) > (self.map_speed * 1.005):
        can_sends.append(create_clu11(self.packer, CS.clu11, Buttons.SET_DECEL, (1 if self.cnt % 9 == 1 else 0)))
      elif (CS.cruise_set_speed * self.speed_conv) < (self.map_speed / 1.005):
        can_sends.append(create_clu11(self.packer, CS.clu11, Buttons.RES_ACCEL, (1 if self.cnt % 9 == 1 else 0)))
      # If nothing needed adjusting, then the speed has been set, which will lock out this control
      else:
        self.speed_adjusted = True

    ### If Driver Overrides using accelerator (or gas for the antiquated), cancel auto speed adjustment
    if CS.pedal_gas:
      self.speed_adjusted = True
    ### Send messages to canbus
    sendcan.send(can_list_to_can_capnp(can_sends, msgtype='sendcan').to_bytes())

    self.cnt += 1
