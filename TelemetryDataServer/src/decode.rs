use serde::Serialize;
pub const PHYSICS_SIZE: usize = 212;
pub const GRAPHICS_SIZE: usize = 1588;
pub const STATIC_SIZE: usize = 200;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Identity {
    pub sm_version: String,
    pub game_version: String,
    pub car: String,
    pub track: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Sample {
    pub physics_packet_id: i32,
    pub graphics_packet_id: i32,
    pub speed_kmh: f32,
    pub rpm: i32,
    pub gear_raw: i32,
    pub throttle: f32,
    pub brake: f32,
    pub steering_raw: f32,
    pub position_x: f32,
    pub position_y: f32,
    pub position_z: f32,
    pub heading_rad: f32,
    pub completed_laps: i32,
    pub lap_time_ms: i32,
    pub progress: f32,
    pub sector: i32,
    pub lap_valid: bool,
    pub session_type: i32,
    pub session_index: i32,
    pub in_pit: bool,
    pub fuel_litres: f32,
    pub clock_seconds: f32,
    pub rain_intensity: i32,
}
pub fn int(b: &[u8], o: usize) -> i32 {
    i32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}
fn float(b: &[u8], o: usize) -> f32 {
    f32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}
fn wide(b: &[u8]) -> String {
    String::from_utf16_lossy(
        &b.chunks_exact(2)
            .map(|p| u16::from_le_bytes([p[0], p[1]]))
            .take_while(|c| *c != 0)
            .collect::<Vec<_>>(),
    )
}
pub fn identity(b: &[u8]) -> Result<Identity, &'static str> {
    if b.len() < STATIC_SIZE {
        return Err("truncated static page");
    }
    Ok(Identity {
        sm_version: wide(&b[0..30]),
        game_version: wide(&b[30..60]),
        car: wide(&b[68..134]),
        track: wide(&b[134..200]),
    })
}
pub fn decode(p: &[u8], g: &[u8]) -> Result<Sample, &'static str> {
    if p.len() < PHYSICS_SIZE || g.len() < GRAPHICS_SIZE {
        return Err("truncated telemetry page");
    }
    if int(g, 4) != 2 {
        return Err("not live");
    }
    let count = int(g, 252);
    if !(1..=60).contains(&count) {
        return Err("invalid active cars");
    }
    let mut matches = (0..count as usize).filter(|i| int(g, 976 + i * 4) == int(g, 1216));
    let i = matches.next().ok_or("player absent")?;
    if matches.next().is_some() {
        return Err("duplicate player id");
    }
    let valid = int(g, 1408);
    if !(0..=1).contains(&valid) {
        return Err("invalid lap validity");
    }
    let s = Sample {
        physics_packet_id: int(p, 0),
        graphics_packet_id: int(g, 0),
        speed_kmh: float(p, 28),
        rpm: int(p, 20),
        gear_raw: int(p, 16),
        throttle: float(p, 4),
        brake: float(p, 8),
        steering_raw: float(p, 24),
        position_x: float(g, 256 + i * 12),
        position_y: float(g, 260 + i * 12),
        position_z: float(g, 264 + i * 12),
        heading_rad: float(p, 208),
        completed_laps: int(g, 132),
        lap_time_ms: int(g, 140),
        progress: float(g, 248),
        sector: int(g, 164),
        lap_valid: valid == 1,
        session_type: int(g, 8),
        session_index: int(g, 1320),
        in_pit: int(g, 160) != 0,
        fuel_litres: float(p, 12),
        clock_seconds: float(g, 1488),
        rain_intensity: int(g, 1560),
    };
    if ![
        s.speed_kmh,
        s.throttle,
        s.brake,
        s.steering_raw,
        s.position_x,
        s.position_y,
        s.position_z,
        s.heading_rad,
        s.progress,
        s.fuel_litres,
        s.clock_seconds,
    ]
    .iter()
    .all(|v| v.is_finite())
    {
        return Err("non-finite data");
    }
    if !(0.0..=1.0).contains(&s.progress)
        || !(0.0..=1.0).contains(&s.throttle)
        || !(0.0..=1.0).contains(&s.brake)
        || s.completed_laps < 0
        || s.lap_time_ms < 0
    {
        return Err("invalid telemetry range");
    }
    Ok(s)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn abi_and_invalid_data() {
        let mut p = [0; PHYSICS_SIZE];
        let mut g = [0; GRAPHICS_SIZE];
        for (o, v) in [
            (4, 2i32),
            (252, 2),
            (976, 7),
            (980, 42),
            (1216, 42),
            (132, 3),
            (140, 12345),
            (164, 2),
            (1320, 8),
            (1408, 1),
        ] {
            g[o..o + 4].copy_from_slice(&v.to_le_bytes());
        }
        for (o, v) in [(248, 0.75f32), (268, 123.0), (272, 4.0), (276, -5.0)] {
            g[o..o + 4].copy_from_slice(&v.to_le_bytes());
        }
        p[208..212].copy_from_slice(&1.2f32.to_le_bytes());
        let s = decode(&p, &g).unwrap();
        assert_eq!(
            (
                s.position_x,
                s.completed_laps,
                s.lap_time_ms,
                s.sector,
                s.session_index
            ),
            (123.0, 3, 12345, 2, 8)
        );
        assert!(s.lap_valid);
        assert_eq!(s.progress, 0.75);
        assert!(decode(&p[..20], &g).is_err());
        for (offset, value) in [(4, 3_i32), (252, 61), (1216, 99), (976, 42), (1408, 2)] {
            let mut invalid = g;
            invalid[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
            assert!(decode(&p, &invalid).is_err());
        }
        p[28..32].copy_from_slice(&f32::NAN.to_le_bytes());
        assert!(decode(&p, &g).is_err());
    }
    #[test]
    fn static_identity_utf16_offsets() {
        let mut data = [0; STATIC_SIZE];
        for (offset, text) in [
            (0, "1.9"),
            (30, "1.7"),
            (68, "bmw_m4_gt3"),
            (134, "brands_hatch"),
        ] {
            for (i, c) in text.encode_utf16().enumerate() {
                data[offset + i * 2..offset + i * 2 + 2].copy_from_slice(&c.to_le_bytes());
            }
        }
        let id = identity(&data).unwrap();
        assert_eq!(id.car, "bmw_m4_gt3");
        assert_eq!(id.track, "brands_hatch");
        assert_eq!(id.sm_version, "1.9");
    }
}
