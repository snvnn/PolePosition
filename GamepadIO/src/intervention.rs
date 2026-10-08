//! Human authority is explicit and never changes the policy weights in flight.
use crate::{Axes, Report};
pub const STEER_BUTTON: u16 = 0x0100; // LB
pub const FULL_BUTTON: u16 = 0x0200; // RB
pub const STOP_BUTTON: u16 = 0x0020; // Back
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
#[cfg(test)] mod tests {
    use super::*;
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
