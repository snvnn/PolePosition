//! Raw XInput semantics; no deadzone, gamma, assists or wheel scaling here.
pub use poleposition_vjoy::{Axes, Result, hash};
use serde::{Deserialize, Serialize};
pub const SEMANTICS: &str = "gamepad_v1";

#[repr(C)]
#[derive(Debug, Default, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Report {
    pub buttons: u16,
    pub lt: u8,
    pub rt: u8,
    pub lx: i16,
    pub ly: i16,
    pub rx: i16,
    pub ry: i16,
}
pub fn normalized(v: i16) -> f64 {
    v as f64 / if v < 0 { 32768. } else { 32767. }
}
impl Report {
    pub fn axes(self) -> Axes {
        Axes {
            steering: normalized(self.lx),
            throttle: self.rt as f64 / 255.,
            brake: self.lt as f64 / 255.,
        }
    }
    pub fn from_axes(axes: Axes) -> Result<Self> {
        axes.validate()?;
        Ok(Self {
            lx: (axes.steering * if axes.steering < 0. { 32768. } else { 32767. }).round() as i16,
            rt: (axes.throttle * 255.).round() as u8,
            lt: (axes.brake * 255.).round() as u8,
            ..Self::default()
        })
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InputRow {
    pub sequence: u64,
    pub segment_id: u64,
    pub read_start_ns: u64,
    pub read_end_ns: u64,
    pub slot: u32,
    pub connected: bool,
    pub packet: u32,
    pub buttons: u16,
    pub lx: i16,
    pub ly: i16,
    pub rx: i16,
    pub ry: i16,
    pub lt: u8,
    pub rt: u8,
    pub stick_x: f64,
    pub stick_y: f64,
    pub right_x: f64,
    pub right_y: f64,
    pub throttle: f64,
    pub brake: f64,
    pub normalization_version: u32,
}
impl InputRow {
    pub fn new(
        sequence: u64,
        segment_id: u64,
        start: u64,
        end: u64,
        slot: u32,
        state: Option<(u32, Report)>,
    ) -> Self {
        let (packet, r) = state.unwrap_or_default();
        let a = r.axes();
        Self {
            sequence,
            segment_id,
            read_start_ns: start,
            read_end_ns: end,
            slot,
            connected: state.is_some(),
            packet,
            buttons: r.buttons,
            lx: r.lx,
            ly: r.ly,
            rx: r.rx,
            ry: r.ry,
            lt: r.lt,
            rt: r.rt,
            stick_x: a.steering,
            stick_y: normalized(r.ly),
            right_x: normalized(r.rx),
            right_y: normalized(r.ry),
            throttle: a.throttle,
            brake: a.brake,
            normalization_version: 1,
        }
    }
}
#[cfg(windows)]
pub mod native;
#[cfg(windows)]
pub mod isolation;
pub mod profile;
pub mod intervention;
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn full_range_roundtrip_and_overlap() {
        for v in i16::MIN..=i16::MAX {
            let r = Report::from_axes(Axes {
                steering: normalized(v),
                throttle: 1.,
                brake: 1.,
            })
            .unwrap();
            assert_eq!(r.lx, v);
            assert_eq!((r.rt, r.lt), (255, 255));
        }
        for v in 0..=255u8 {
            assert_eq!(
                Report::from_axes(Axes {
                    steering: 0.,
                    throttle: v as f64 / 255.,
                    brake: 0.
                })
                .unwrap()
                .rt,
                v
            );
        }
        assert!(
            Report::from_axes(Axes {
                steering: f64::NAN,
                ..Axes::NEUTRAL
            })
            .is_err()
        );
        assert_eq!(std::mem::size_of::<Report>(), 12);
    }
    #[test]
    fn repeated_packet_is_connected() {
        let row = InputRow::new(1, 1, 5, 6, 0, Some((0, Report::default())));
        assert!(row.connected);
        assert_eq!(row.packet, 0);
        assert!(!InputRow::new(2, 1, 7, 8, 0, None).connected);
    }
}
