//! Additional gate only for explicitly confirmed session-free main-menu setup.
#[derive(Default)]
pub struct MainMenuGate {
    absent_since: Option<u64>,
    blocked: bool,
}
impl MainMenuGate {
    pub fn update(&mut self, now_ns: u64, absent: Option<bool>, foreground: bool) -> bool {
        if absent != Some(true) {
            self.blocked = true;
        }
        if self.blocked {
            return false;
        }
        let since = *self.absent_since.get_or_insert(now_ns);
        foreground && now_ns.saturating_sub(since) >= 500_000_000
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn main_menu_requires_absence_dwell_and_foreground() {
        let mut gate = MainMenuGate::default();
        assert!(!gate.update(0, Some(true), true));
        assert!(!gate.update(499_999_999, Some(true), true));
        assert!(!gate.update(500_000_000, Some(true), false));
        assert!(gate.update(510_000_000, Some(true), true));
    }
    #[test]
    fn session_appearance_blocks_even_after_it_disappears() {
        let mut gate = MainMenuGate::default();
        gate.update(0, Some(true), true);
        assert!(gate.update(500_000_000, Some(true), true));
        assert!(!gate.update(510_000_000, Some(false), true));
        assert!(!gate.update(2_000_000_000, Some(true), true));
    }
    #[test]
    fn query_failure_and_existing_session_are_not_absence() {
        for value in [None, Some(false)] {
            let mut gate = MainMenuGate::default();
            assert!(!gate.update(0, value, true));
            assert!(!gate.update(1_000_000_000, Some(true), true));
        }
    }
}
