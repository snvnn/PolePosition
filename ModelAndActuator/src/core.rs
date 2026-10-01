//! Pure guardian state machine; the producer has no access to the vJoy handle.
use crate::{Axes, Config};
use serde::{Deserialize, Serialize};
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Command {
    pub sequence: u64,
    pub created_ns: u64,
    pub expires_ns: u64,
    pub observation_sequence: u64,
    pub observation_ns: u64,
    pub tracking_epoch: u64,
    pub steering: f64,
    pub throttle: f64,
    pub brake: f64,
}
impl Command {
    pub fn axes(&self) -> Axes {
        Axes {
            steering: self.steering,
            throttle: self.throttle,
            brake: self.brake,
        }
    }
}
#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct Health {
    #[serde(default)]
    pub rpm: i32,
    pub observation_sequence: u64,
    pub observation_ns: u64,
    pub physics_ns: u64,
    pub graphics_ns: u64,
    pub tracking_epoch: u64,
    pub speed_kmh: f64,
    /// ACC encoding: 0 reverse, 1 neutral, 2 first forward gear.
    pub gear_raw: i32,
    pub ready: bool,
    pub live: bool,
    pub identity_ok: bool,
    pub foreground: bool,
    /// Fresh, direct paused/off status or independently verified absence of the game process.
    pub game_halted: bool,
}
impl Health {
    pub fn fresh(&self, now: u64, age: u64) -> bool {
        self.live
            && self.observation_sequence > 0
            && self.speed_kmh.is_finite()
            && self.speed_kmh >= 0.
            && [self.observation_ns, self.physics_ns, self.graphics_ns]
                .iter()
                .all(|&t| t <= now && now - t < age)
    }
    pub fn allowed(&self, now: u64, c: &Config) -> bool {
        self.fresh(now, c.age_ns()) && self.ready && self.identity_ok && self.foreground
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
pub enum Mode {
    Waiting,
    Running,
    Stopping,
    Finished,
}
#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    None,
    Send(Axes),
    Release,
}
#[derive(Default)]
pub struct Signals {
    pub start: bool,
    pub stop: bool,
    pub producer_lost: bool,
    pub log_failed: bool,
    pub source_changed: bool,
    pub explicit_release: bool,
}
pub struct Guard {
    pub config: Config,
    pub mode: Mode,
    pub reason: String,
    pub armed_ns: Option<u64>,
    pub stop_ns: Option<u64>,
    pub first_zero_sent_ns: Option<u64>,
    pub stop_confirmed: bool,
    pub released_while_halted: bool,
    command: Option<Command>,
    last_sequence: Option<u64>,
    last_created: Option<u64>,
    last_tick: Option<u64>,
    output: Axes,
    stopped_since: Option<u64>,
    last_stop_observation: u64,
    stop_sent: bool,
    armed_epoch: Option<u64>,
}
impl Guard {
    pub fn new(config: Config) -> Self {
        Self {
            config,
            mode: Mode::Waiting,
            reason: String::new(),
            armed_ns: None,
            stop_ns: None,
            first_zero_sent_ns: None,
            stop_confirmed: false,
            released_while_halted: false,
            command: None,
            last_sequence: None,
            last_created: None,
            last_tick: None,
            output: Axes::NEUTRAL,
            stopped_since: None,
            last_stop_observation: 0,
            stop_sent: false,
            armed_epoch: None,
        }
    }
    pub fn accept(
        &mut self,
        c: Command,
        now: u64,
        h: &Health,
    ) -> std::result::Result<(), &'static str> {
        c.axes().validate().map_err(|_| "invalid_axes")?;
        if c.sequence == 0
            || self.last_sequence.is_some_and(|v| c.sequence <= v)
            || self.last_created.is_some_and(|v| c.created_ns < v)
        {
            return Err("command_order");
        }
        if c.created_ns > now
            || c.expires_ns <= now
            || c.expires_ns <= c.created_ns
            || c.expires_ns - c.created_ns > self.config.age_ns()
            || now - c.created_ns >= self.config.age_ns()
        {
            return Err("command_expired");
        }
        if c.observation_sequence == 0
            || c.observation_sequence > h.observation_sequence
            || c.tracking_epoch != h.tracking_epoch
            || c.observation_ns > h.observation_ns
            || c.observation_ns > now
            || now - c.observation_ns >= self.config.age_ns()
        {
            return Err("command_provenance");
        }
        self.last_sequence = Some(c.sequence);
        self.last_created = Some(c.created_ns);
        self.command = Some(c);
        Ok(())
    }
    pub fn stop(&mut self, now: u64, reason: &str) {
        if self.mode == Mode::Finished || self.mode == Mode::Stopping {
            return;
        }
        // A late rejected reply or user event must not hide an already expired command.
        let trigger = if self.mode == Mode::Running {
            self.command.as_ref().map_or(now, |c| {
                now.min(c.expires_ns)
                    .min(c.observation_ns.saturating_add(self.config.age_ns()))
            })
        } else {
            now
        };
        self.mode = Mode::Stopping;
        self.reason = reason.into();
        self.stop_ns = Some(trigger);
        self.stopped_since = None;
        self.last_stop_observation = 0;
        self.command = None;
    }
    pub fn command(&self) -> Option<&Command> {
        self.command.as_ref()
    }
    pub fn acknowledge(&mut self, now: u64, axes: Axes) {
        self.output = axes;
        if self.mode == Mode::Stopping && axes == Axes::STOP {
            self.stop_sent = true;
            if self.first_zero_sent_ns.is_none() {
                self.first_zero_sent_ns = Some(now);
            }
        }
    }
    fn stationary(&mut self, now: u64, h: &Health) -> bool {
        if !h.fresh(now, self.config.age_ns()) || h.speed_kmh >= self.config.stopped_kmh {
            self.stopped_since = None;
            return false;
        }
        if h.observation_sequence != self.last_stop_observation {
            self.last_stop_observation = h.observation_sequence;
            let start = *self.stopped_since.get_or_insert(h.observation_ns);
            return h.observation_ns.saturating_sub(start)
                >= (self.config.stopped_seconds * 1e9) as u64;
        }
        false
    }
    pub fn tick(&mut self, now: u64, h: &Health, s: Signals) -> Action {
        let dt = self
            .last_tick
            .map(|t| now.saturating_sub(t) as f64 / 1e9)
            .unwrap_or(0.0)
            .min(self.config.max_age_ms / 1000.);
        if self.last_tick.is_some_and(|t| now < t) {
            self.stop(now, "clock_reversal");
        }
        if self
            .last_tick
            .is_some_and(|t| now.saturating_sub(t) >= self.config.age_ns())
        {
            self.stopped_since = None;
        }
        self.last_tick = Some(now);
        if self.mode == Mode::Finished {
            return Action::None;
        }
        if s.stop {
            self.stop(now, "user_stop");
        } else if s.log_failed {
            self.stop(now, "logging_failure");
        } else if s.producer_lost {
            self.stop(now, "producer_lost");
        }
        if self.mode == Mode::Waiting {
            let stationary = self.stationary(now, h);
            if s.start
                && h.allowed(now, &self.config)
                && stationary
                && self.command.as_ref().is_some_and(|c| c.expires_ns > now)
            {
                self.mode = Mode::Running;
                self.armed_ns = Some(now);
                self.armed_epoch = Some(h.tracking_epoch);
                self.stopped_since = None;
            } else {
                return Action::None;
            }
        }
        if self.mode == Mode::Running {
            let reason = if s.source_changed || Some(h.tracking_epoch) != self.armed_epoch {
                Some("source_changed")
            } else if !h.foreground {
                Some("acc_not_foreground")
            } else if !h.fresh(now, self.config.age_ns()) {
                Some("telemetry_expired")
            } else if !h.identity_ok {
                Some("identity_mismatch")
            } else if !h.ready {
                Some("tracking_lost")
            } else if h.speed_kmh > self.config.max_speed_kmh {
                Some("speed_limit")
            } else if now.saturating_sub(self.armed_ns.unwrap())
                >= (self.config.test_seconds * 1e9) as u64
            {
                Some("time_limit")
            } else if self.command.as_ref().is_none_or(|c| {
                c.expires_ns <= now || now.saturating_sub(c.observation_ns) >= self.config.age_ns()
            }) {
                Some("command_expired")
            } else {
                None
            };
            if let Some(r) = reason {
                let trigger = match r {
                    "command_expired" => self.command.as_ref().map_or(now, |c| {
                        c.expires_ns
                            .min(c.observation_ns.saturating_add(self.config.age_ns()))
                    }),
                    "telemetry_expired" if h.live => {
                        [h.observation_ns, h.physics_ns, h.graphics_ns]
                            .into_iter()
                            .min()
                            .unwrap()
                            .saturating_add(self.config.age_ns())
                            .min(now)
                    }
                    "time_limit" => self
                        .armed_ns
                        .unwrap()
                        .saturating_add((self.config.test_seconds * 1e9) as u64),
                    _ => now,
                };
                self.stop(trigger, r);
            }
        }
        if self.mode == Mode::Stopping {
            if self.armed_ns.is_none() {
                // Before F7, no non-neutral input is authorized, including emergency braking.
                self.mode = Mode::Finished;
                return Action::Release;
            }
            let stopped = self.stationary(now, h);
            if self.stop_sent && (stopped || h.game_halted || s.explicit_release) {
                self.stop_confirmed = stopped;
                self.released_while_halted = !stopped;
                self.mode = Mode::Finished;
                return Action::Release;
            }
            return Action::Send(Axes::STOP);
        }
        let c = self.command.as_ref().unwrap();
        let target = Axes {
            steering: c
                .steering
                .clamp(-self.config.max_steering, self.config.max_steering),
            throttle: if c.brake > 0. {
                0.
            } else {
                c.throttle.min(self.config.max_throttle)
            },
            brake: c.brake,
        };
        let ramp = |old: f64, new: f64, rate: f64| old + (new - old).clamp(-rate * dt, rate * dt);
        let brake = ramp(self.output.brake, target.brake, self.config.brake_rate);
        // Never leak residual throttle into a brake request during rate limiting.
        let axes = Axes {
            steering: ramp(
                self.output.steering,
                target.steering,
                self.config.steering_rate,
            ),
            throttle: if target.brake > 0. || brake > 0. {
                0.
            } else {
                ramp(
                    self.output.throttle,
                    target.throttle,
                    self.config.throttle_rate,
                )
            },
            brake,
        };
        Action::Send(axes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn cfg() -> Config {
        toml::from_str(include_str!("../config.toml")).unwrap()
    }
    fn health(t: u64) -> Health {
        Health {
            observation_sequence: t / 10_000_000 + 1,
            observation_ns: t,
            physics_ns: t,
            graphics_ns: t,
            ready: true,
            live: true,
            identity_ok: true,
            foreground: true,
            ..Default::default()
        }
    }
    fn cmd(t: u64, h: &Health) -> Command {
        Command {
            sequence: t + 1,
            created_ns: t,
            expires_ns: t + 100_000_000,
            observation_sequence: h.observation_sequence,
            observation_ns: h.observation_ns,
            tracking_epoch: h.tracking_epoch,
            steering: 0.1,
            throttle: 0.2,
            brake: 0.,
        }
    }
    fn armed() -> Guard {
        let mut g = Guard::new(cfg());
        for t in (0..=60).map(|i| i * 10_000_000) {
            let h = health(t);
            g.accept(cmd(t, &h), t, &h).unwrap();
            if let Action::Send(a) = g.tick(
                t,
                &h,
                Signals {
                    start: t == 600_000_000,
                    ..Default::default()
                },
            ) {
                g.acknowledge(t, a);
            }
        }
        assert_eq!(g.mode, Mode::Running);
        g
    }
    #[test]
    fn unarmed_no_output() {
        let mut g = Guard::new(cfg());
        assert_eq!(g.tick(1, &health(1), Signals::default()), Action::None);
    }
    #[test]
    fn unarmed_failure_never_applies_brake() {
        let mut g = Guard::new(cfg());
        assert_eq!(
            g.tick(
                1,
                &Health::default(),
                Signals {
                    producer_lost: true,
                    ..Default::default()
                }
            ),
            Action::Release
        );
        assert_eq!(g.mode, Mode::Finished);
        assert!(!g.stop_confirmed);
    }
    #[test]
    fn command_validation() {
        let h = health(100);
        let mut g = Guard::new(cfg());
        let mut c = cmd(100, &h);
        c.throttle = f64::NAN;
        assert_eq!(g.accept(c, 100, &h), Err("invalid_axes"));
        let c = cmd(100, &h);
        g.accept(c.clone(), 100, &h).unwrap();
        assert_eq!(g.accept(c.clone(), 100, &h), Err("command_order"));
        let mut c = c;
        c.sequence += 1;
        c.expires_ns = 100;
        assert_eq!(g.accept(c, 100, &h), Err("command_expired"));
    }
    #[test]
    fn brake_priority_and_rate() {
        let mut g = armed();
        let h = health(610_000_000);
        let mut c = cmd(610_000_000, &h);
        c.brake = 0.5;
        g.accept(c, 610_000_000, &h).unwrap();
        let Action::Send(a) = g.tick(610_000_000, &h, Signals::default()) else {
            panic!()
        };
        assert_eq!(a.throttle, 0.);
        assert!(a.brake <= 0.02000001);
    }
    #[test]
    fn emergency_bypasses_rates_no_rearm() {
        let mut g = armed();
        let h = health(610_000_000);
        assert_eq!(
            g.tick(
                610_000_000,
                &h,
                Signals {
                    stop: true,
                    ..Default::default()
                }
            ),
            Action::Send(Axes::STOP)
        );
        g.acknowledge(610_000_000, Axes::STOP);
        for t in (62..=112).map(|i| i * 10_000_000) {
            g.tick(
                t,
                &health(t),
                Signals {
                    start: true,
                    ..Default::default()
                },
            );
        }
        assert_eq!(g.mode, Mode::Finished);
        assert!(g.stop_confirmed);
        assert_eq!(
            g.tick(
                1_200_000_000,
                &health(1_200_000_000),
                Signals {
                    start: true,
                    ..Default::default()
                }
            ),
            Action::None
        );
    }
    #[test]
    fn stale_holds_brake() {
        let mut g = armed();
        let h = health(600_000_000);
        assert_eq!(
            g.tick(710_000_000, &h, Signals::default()),
            Action::Send(Axes::STOP)
        );
        g.acknowledge(710_000_000, Axes::STOP);
        assert_eq!(
            g.tick(5_000_000_000, &h, Signals::default()),
            Action::Send(Axes::STOP)
        );
        assert!(!g.stop_confirmed);
    }
    #[test]
    fn producer_lost_and_log_failed() {
        for log in [false, true] {
            let mut g = armed();
            assert_eq!(
                g.tick(
                    610_000_000,
                    &health(610_000_000),
                    Signals {
                        producer_lost: !log,
                        log_failed: log,
                        ..Default::default()
                    }
                ),
                Action::Send(Axes::STOP)
            );
        }
    }
    #[test]
    fn paused_can_release_after_brake() {
        let mut g = armed();
        let mut h = health(610_000_000);
        h.live = false;
        h.game_halted = true;
        assert_eq!(
            g.tick(610_000_000, &h, Signals::default()),
            Action::Send(Axes::STOP)
        );
        g.acknowledge(610_000_000, Axes::STOP);
        assert_eq!(g.tick(620_000_000, &h, Signals::default()), Action::Release);
        assert!(!g.stop_confirmed);
    }
    #[test]
    fn start_requires_continuous_fresh_stationary() {
        let mut g = Guard::new(cfg());
        let h = health(0);
        g.tick(0, &h, Signals::default());
        g.tick(200_000_000, &h, Signals::default());
        let h = health(600_000_000);
        g.accept(cmd(600_000_000, &h), 600_000_000, &h).unwrap();
        assert_eq!(
            g.tick(
                600_000_000,
                &h,
                Signals {
                    start: true,
                    ..Default::default()
                }
            ),
            Action::None
        );
    }
    #[test]
    fn focus_speed_and_tracking_stop() {
        for kind in 0..3 {
            let mut g = armed();
            let mut h = health(610_000_000);
            match kind {
                0 => h.foreground = false,
                1 => h.speed_kmh = 21.,
                _ => h.ready = false,
            }
            assert_eq!(
                g.tick(610_000_000, &h, Signals::default()),
                Action::Send(Axes::STOP)
            );
        }
    }
    #[test]
    fn invalid_configuration() {
        let mut c = cfg();
        c.validate().unwrap();
        c.max_throttle = 1.;
        assert!(c.validate().is_err());
    }
    #[test]
    fn source_epoch_and_expired_pages_stop() {
        for field in 0..3 {
            let mut g = armed();
            let now = 610_000_000;
            let mut h = health(now);
            match field {
                0 => h.tracking_epoch += 1,
                1 => h.physics_ns = now - 100_000_000,
                _ => h.graphics_ns = now - 100_000_000,
            };
            assert_eq!(
                g.tick(now, &h, Signals::default()),
                Action::Send(Axes::STOP)
            );
        }
    }
    #[test]
    fn expired_command_uses_deadline_for_latency() {
        let mut g = armed();
        let deadline = g.command().unwrap().expires_ns;
        let now = deadline + 20_000_000;
        assert_eq!(
            g.tick(now, &health(now), Signals::default()),
            Action::Send(Axes::STOP)
        );
        assert_eq!(g.stop_ns, Some(deadline));
    }
    #[test]
    fn future_and_unrelated_commands_rejected() {
        let mut g = Guard::new(cfg());
        let h = health(100);
        let mut c = cmd(100, &h);
        c.created_ns = 101;
        assert_eq!(g.accept(c, 100, &h), Err("command_expired"));
        let mut c = cmd(100, &h);
        c.tracking_epoch = 8;
        assert_eq!(g.accept(c, 100, &h), Err("command_provenance"));
    }
    #[test]
    fn moving_or_unready_cannot_arm() {
        for field in 0..3 {
            let mut g = Guard::new(cfg());
            for i in 0..70 {
                let now = i * 10_000_000;
                let mut h = health(now);
                match field {
                    0 => h.speed_kmh = 1.,
                    1 => h.ready = false,
                    _ => h.foreground = false,
                };
                g.accept(cmd(now, &h), now, &h).unwrap();
                assert_eq!(
                    g.tick(
                        now,
                        &h,
                        Signals {
                            start: true,
                            ..Default::default()
                        }
                    ),
                    Action::None
                );
            }
        }
    }
    #[test]
    fn repeated_old_observation_never_confirms_stop() {
        let mut g = armed();
        let now = 610_000_000;
        let h = health(now);
        g.stop(now, "test");
        g.acknowledge(now, Axes::STOP);
        for i in 0..60 {
            assert_eq!(
                g.tick(now + i * 10_000_000, &h, Signals::default()),
                Action::Send(Axes::STOP)
            );
        }
        assert!(!g.stop_confirmed);
    }

    #[test]
    fn long_run_and_wider_steering_require_explicit_lap_config() {
        let mut c = cfg();
        c.test_seconds = 3600.;
        c.max_steering = 0.35;
        assert!(c.validate().is_err());
        let baseline = toml::from_str(include_str!("../../BaselineController/lap.toml")).unwrap();
        c.baseline = Some(crate::Demo {
            prepared: "verified-at-load".into(),
            config: baseline,
        });
        c.validate().unwrap();
        c.test_seconds = 3601.;
        assert!(c.validate().is_err());
    }
}
