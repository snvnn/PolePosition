"""Run reviewed unit/mock checks, or list checks requiring separate environments."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


UNIT = (
    'ExecutionRuntime.test_timing',
    'TrainingController.test_task_contract',
    'TrainingController.test_coordinator.CoordinatorTests',
    'TrainingController.test_management.ProtocolTests',
    'TrainingController.test_management.JobTests',
    'TrainingController.test_results',
    'TrainingController.test_result_deletion',
    'TrainingController.test_worker_failure',
    'TrainingEnvironment.test_acc_reset',
    'TrainingEnvironment.test_auto_cycle',
    'TrainingEnvironment.test_champion_cycle',
    'TrainingEnvironment.test_policy_comparison',
    'TrainingEnvironment.test_gamepad_online',
    'TrainingEnvironment.test_gamepad_recovery',
    'TrainingEnvironment.test_steering_smoothness',
    'TrainingEnvironment.test_run_dependencies',
)
MOCK_PROCESS = (
    'TrainingController.test_task_catalog',
    'TrainingController.test_management.ProcessTests',
    'TrainingController.test_management.GuiTests',
    'TrainingController.test_gamepad',
)
# These have additional native binaries, recorded assets or broader workflow
# prerequisites. They are discoverable here but never included in the defaults.
LOCAL_ASSETS = (
    'TrainingController.test_coordinator.MonitorIntegrationTests',
    'TrainingController.test_preparation',
    'TrainingController.test_rust_management',
    'TrainingController.test_shadow',
    'TrainingController.test_online',
    'TrainingController.test_workflow',
    'TrainingController.test_v2_workflow',
    'ModelTrainer.test_training',
    'TrainingEnvironment.test_pretrain',
    'StateProcessor.test_state',
    'TrainingEnvironment.test_checkpoint',
    'TrainingEnvironment.test_environment',
    'TrainingEnvironment.test_online',
    'TrainingEnvironment.test_policy',
    'TrainingEnvironment.test_gamepad',
)
OTHER_UNIT = (
    'DatasetBuilder.test_builder', 'DatasetBuilder.test_report', 'DatasetBuilder.test_review',
    'DrivingRecorder.test_validate_recording', 'Evaluator.test_live',
    'GamepadIO.test_isolation', 'GamepadIO.test_reports', 'GamepadIO.test_validation',
    'ModelAndActuator.test_actuator', 'TrackModel.test_model',
    'TrainingEnvironment.test_augmentation', 'TrainingEnvironment.test_continuous_reference',
    'TrainingEnvironment.test_controls_inventory', 'TrainingEnvironment.test_corrections',
    'TrainingEnvironment.test_evaluation', 'TrainingEnvironment.test_human_imitation',
    'TrainingEnvironment.test_lap_gamepad', 'TrainingEnvironment.test_lap_report',
    'TrainingEnvironment.test_lifecycle', 'TrainingEnvironment.test_outcome_learning',
    'TrainingEnvironment.test_response_regularization', 'TrainingEnvironment.test_steering_feedback',
    'TrainingEnvironment.test_successful_rollouts', 'TrainingEnvironment.test_teacher',
)
DEVICE = (
    'VirtualJoyStick/src/native.rs: ignored installed-driver check',
    'poleposition-gamepad bench / intervention-bench / response / live drive (manual)',
)


def inventory():
    groups = {'unit': UNIT, 'mock_process': MOCK_PROCESS, 'local_assets': LOCAL_ASSETS, 'device': DEVICE}
    for name, entries in groups.items():
        print(name + ':')
        for entry in entries:
            print('  ' + entry)
    print('unit (additional domain checks, outside the default execution-refactor suite):')
    for entry in OTHER_UNIT:
        print('  ' + entry)
    reviewed = {'.'.join(entry.split('.')[:2]) for entry in UNIT + MOCK_PROCESS + LOCAL_ASSETS + OTHER_UNIT}
    root = Path(__file__).resolve().parents[1]
    for folder in sorted(root.iterdir()):
        if folder.is_dir() and not folder.name.startswith('.'):
            for path in sorted(folder.glob('test_*.py')):
                module = folder.name + '.' + path.stem
                if module not in reviewed:
                    raise ValueError('Classify this test before adding it to the inventory: ' + module)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('offline', 'unit', 'mock_process'), default='offline')
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()
    if args.list:
        inventory()
        return 0
    names = UNIT + MOCK_PROCESS if args.suite == 'offline' else UNIT if args.suite == 'unit' else MOCK_PROCESS
    # Enforce the offline boundary even if a fixture accidentally stops injecting
    # its native/reset port. Real process monitoring/device/menu access is forbidden.
    with ExitStack() as stack:
        for target in ('TrainingEnvironment.game_process.GameProcess.__init__',
                       'TrainingEnvironment.acc_reset.WindowsPort.__init__'):
            stack.enter_context(patch(target, side_effect=AssertionError('Live adapter in offline suite: ' + target)))
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
