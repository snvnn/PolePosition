//! Human authority is explicit and never changes the policy weights in flight.
use crate::{Axes, Report};
pub const STEER_BUTTON: u16 = 0x0100; // LB
pub const FULL_BUTTON: u16 = 0x0200; // RB
pub const STOP_BUTTON: u16 = 0x0020; // Back
pub fn confirms_teacher_selection(teacher: Report, shift_mask: u16) -> bool {
    teacher.buttons & STOP_BUTTON == 0
        && (teacher.lx.unsigned_abs() > 8192 || shift_buttons(teacher, shift_mask, true) != 0)
}
/// Relay parked device-selection input without enabling throttle before F7.
pub fn waiting_axes(teacher: Report, allowed: bool, speed_kmh: f64) -> Axes {
    if allowed && speed_kmh.is_finite() && (0.0..=1.0).contains(&speed_kmh)
        && teacher.buttons & STOP_BUTTON == 0 {
        Axes { steering: teacher.axes().steering, throttle: 0.0, brake: teacher.axes().brake }
    } else { Axes::NEUTRAL }
}
#[derive(Debug, Clone, Copy, PartialEq, serde::Serialize)]
#[serde(rename_all="snake_case")]
pub enum Authority { Model, HumanSteer, HumanFull, Emergency }
#[derive(Debug,Clone,Copy)]
pub struct Choice { pub axes: Axes, pub authority: Authority, pub label_mask:[u8;3] }
pub fn choose(model:Axes, teacher:Report)->Choice {
    let human=teacher.axes();
    if teacher.buttons & STOP_BUTTON != 0 {
        Choice{axes:Axes::STOP,authority:Authority::Emergency,label_mask:[0,0,0]}
    } else if teacher.buttons & FULL_BUTTON != 0 {
        Choice{axes:human,authority:Authority::HumanFull,label_mask:[1,1,1]}
    } else if teacher.buttons & STEER_BUTTON != 0 {
        Choice{axes:Axes{steering:human.steering,..model},authority:Authority::HumanSteer,label_mask:[1,0,0]}
    } else {Choice{axes:model,authority:Authority::Model,label_mask:[0,0,0]}}
}
/// Sample exactly once per raw input, never once per policy/send call.
#[derive(Default)]
pub struct Toggle { pub active: bool, previous_rb: bool }
impl Toggle {
    pub fn update(&mut self, buttons: u16, enabled: bool) {
        let rb = buttons & FULL_BUTTON != 0;
        if !enabled || buttons & STOP_BUTTON != 0 { self.active = false; }
        else if rb && !self.previous_rb { self.active = !self.active; }
        self.previous_rb = rb;
    }
    pub fn choose(&self, model: Axes, mut teacher: Report) -> Choice {
        teacher.buttons &= !FULL_BUTTON;
        if self.active { teacher.buttons |= FULL_BUTTON; }
        choose(model, teacher)
    }
}
#[cfg(test)] mod tests {
    use super::*;
    #[test] fn gear_button_confirms_teacher_without_stick_motion() {
        assert!(confirms_teacher_selection(Report{buttons:0x1000,..Default::default()},0x5000));
        assert!(confirms_teacher_selection(Report{buttons:0x4000,..Default::default()},0x5000));
        for buttons in [0, FULL_BUTTON, STEER_BUTTON, 0x2000, 0x1000|STOP_BUTTON] {
            assert!(!confirms_teacher_selection(Report{buttons,..Default::default()},0x5000));
        }
        assert!(confirms_teacher_selection(Report{lx:16000,..Default::default()},0x5000));
    }
    #[test] fn waiting_can_select_pad_but_never_accelerates() {
        let p=Report{lx:16000,rt:255,lt:100,..Default::default()};
        let a=waiting_axes(p,true,0.0);
        assert!(a.steering>0.0);assert!(a.brake>0.0);assert_eq!(a.throttle,0.0);
        for (allowed,speed) in [(false,0.0),(true,2.0),(true,f64::NAN)] {
            assert_eq!(waiting_axes(p,allowed,speed),Axes::NEUTRAL);
        }
        assert_eq!(waiting_axes(Report{buttons:STOP_BUTTON,..p},true,0.0),Axes::NEUTRAL);
    }
    #[test] fn toggle_edges_reset_and_emergency() {
        let mut t=Toggle::default();let model=Axes::NEUTRAL;
        t.update(FULL_BUTTON,false);t.update(FULL_BUTTON,true);assert!(!t.active);
        t.update(0,true);t.update(FULL_BUTTON,true);assert!(t.active);
        for _ in 0..100 {t.update(FULL_BUTTON,true);assert!(t.active);}
        t.update(0,true);assert_eq!(t.choose(model,Report::default()).authority,Authority::HumanFull);
        t.update(FULL_BUTTON,true);assert!(!t.active);
        t.update(0,true);t.update(FULL_BUTTON,true);t.update(STOP_BUTTON,true);
        assert!(!t.active);assert_eq!(t.choose(model,Report{buttons:STOP_BUTTON,..Default::default()}).axes,Axes::STOP);
        t.update(0,true);t.update(FULL_BUTTON,true);t.update(0,false);assert!(!t.active);
    }
    #[test] fn takeover_masks_priorities_and_release() {
        let model=Axes{steering:-0.4,throttle:0.8,brake:0.1};
        let mut pad=Report{lx:16384,rt:64,lt:128,buttons:STEER_BUTTON,..Default::default()};
        let c=choose(model,pad);assert_eq!(c.label_mask,[1,0,0]);assert_eq!(c.axes.throttle,0.8);assert_eq!(c.axes.brake,0.1);
        pad.buttons|=FULL_BUTTON;let c=choose(model,pad);assert_eq!(c.label_mask,[1,1,1]);assert_eq!(c.axes,pad.axes());
        pad.buttons|=STOP_BUTTON;assert_eq!(choose(model,pad).axes,Axes::STOP);
        pad.buttons=0;assert_eq!(choose(model,pad).axes,model);
        assert_eq!(Report::from_axes(c.axes).unwrap().buttons,0);
    }
}

/// Only ready/waiting or running control may relay shifts. Emergency/braking sends none.
pub fn shift_buttons(teacher: Report, mask: u16, allowed: bool) -> u16 {
    if allowed && teacher.buttons & STOP_BUTTON == 0 { teacher.buttons & mask & 0xf000 } else { 0 }
}
#[cfg(test)] mod shift_tests {
    use super::*;
    #[test] fn passthrough_preserves_press_release_and_filters_safety_buttons() {
        let mut p=Report{buttons:0xffff & !STOP_BUTTON,..Default::default()};
        assert_eq!(shift_buttons(p,0x5000,true),0x5000);
        assert_eq!(shift_buttons(p,0x5000,false),0);
        p.buttons=0;assert_eq!(shift_buttons(p,0x5000,true),0);
        p.buttons=0x1000|STOP_BUTTON;assert_eq!(shift_buttons(p,0x5000,true),0);
        p.buttons=FULL_BUTTON|STEER_BUTTON;assert_eq!(shift_buttons(p,0x5000,true),0);
    }
}
