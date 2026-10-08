import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import herdr_team_status as status
import herdr_sidebar as sidebar


def pane(pid, role=None, state='idle', tab='t1'):
    return {'pane_id': pid, 'terminal_id': 'term-' + pid, 'agent': 'agy',
            'agent_status': state, 'workspace_id': 'w1', 'tab_id': tab,
            'tokens': {sidebar.ROLE: role} if role else {}}


class TeamStatusTests(unittest.TestCase):
    def test_finished_indicator_stays_until_agent_is_viewed(self):
        live = [pane('agent')]
        live[0]['completion_seq'] = 10
        seen = {}
        self.assertEqual(status.effective_statuses(live, seen)['agent'], 'done')
        live[0]['focused'] = True
        self.assertEqual(status.effective_statuses(live, seen)['agent'], 'idle')
        live[0]['focused'] = False
        self.assertEqual(status.effective_statuses(live, seen)['agent'], 'idle')
        live[0]['completion_seq'] = 11
        self.assertEqual(status.effective_statuses(live, seen)['agent'], 'done')

    def test_idle_or_done_orchestrator_is_busy_until_last_crew_stops(self):
        panes = [pane('lead', 'lead', 'done'), pane('c1', 'crew', 'working'),
                 pane('c2', 'crew', 'working'), pane('other', 'lead', 'idle', 't2')]
        self.assertEqual(status.effective_statuses(panes)['lead'], 'crew_working')
        self.assertEqual(status.effective_statuses(panes)['other'], 'idle')
        panes[1]['agent_status'] = 'done'
        self.assertEqual(status.effective_statuses(panes)['lead'], 'crew_working')
        panes[2]['agent_status'] = 'idle'
        self.assertEqual(status.effective_statuses(panes)['lead'], 'done')
        panes[0]['agent_status'] = 'idle'
        panes[2]['agent_status'] = 'working'
        self.assertEqual(status.effective_statuses(panes)['lead'], 'crew_working')
        self.assertEqual(panes[0]['agent_status'], 'idle', 'native state must not be overwritten')
        panes[0]['agent_status'] = 'working'
        self.assertEqual(status.effective_statuses(panes)['lead'], 'crew_working')

    def test_blocked_orchestrator_keeps_attention_indicator(self):
        panes = [pane('lead', 'lead', 'blocked'), pane('crew', 'crew', 'working')]
        self.assertEqual(status.effective_statuses(panes)['lead'], 'blocked')

    def test_status_reports_are_display_only_and_refresh_ttl(self):
        panes = [pane('lead', 'lead', 'done'), pane('crew', 'crew', 'working')]
        refreshed, call = {}, Mock()
        status.publish(panes, refreshed, call, now=0)
        lead = call.call_args_list[0].args[1]
        self.assertEqual(lead['tokens']['herdr_ai_icon_crew_working'], '●')
        self.assertIsNone(lead['tokens']['herdr_ai_icon_done'])
        self.assertEqual(lead['tokens']['herdr_ai_status'], 'working')
        self.assertEqual(lead['tokens']['herdr_ai_priority'], '2')
        self.assertEqual(lead['ttl_ms'], 10000)
        self.assertTrue(all(c.args[0] == 'pane.report_metadata' for c in call.call_args_list))
        call.reset_mock()
        status.publish(panes, refreshed, call, now=1)
        call.assert_not_called()
        status.publish(panes, refreshed, call, now=5)
        self.assertEqual(call.call_count, 2)
        panes[1]['agent_status'] = 'done'
        call.reset_mock()
        status.publish(panes, refreshed, call, now=6)
        self.assertEqual(call.call_args_list[0].args[1]['tokens']['herdr_ai_icon_done'], '●')
        self.assertIsNone(call.call_args_list[0].args[1]['tokens']['herdr_ai_icon_crew_working'])

    def test_symbol_style_and_replaced_terminal_refresh_immediately(self):
        live = [pane('agent', state='working')]
        refreshed, call = {}, Mock()
        status.publish(live, refreshed, call, now=0, symbols=True)
        self.assertEqual(call.call_args.args[1]['tokens']['herdr_ai_icon_working'], '◐')
        live[0]['terminal_id'] = 'replacement'
        call.reset_mock()
        status.publish(live, refreshed, call, now=1, symbols=True)
        call.assert_called_once()

    def test_config_keeps_task_labels_keys_and_theme_and_is_idempotent(self):
        original = '''[theme]
name = "gruvbox"
[ui]
agent_panel_sort = "spaces"
status_indicators = "dots"
[ui.sidebar.agents]
rows = [["state_icon", "machine", "workspace", "tab"], ["agent"], ["$task_label_tail"]]
[keys]
zoom = "prefix+z"
'''
        changed = status.indicator_config(original)
        self.assertIn('fg = "#fe8019"', changed)
        self.assertIn('["$task_label_tail"]', changed)
        self.assertIn('[keys]\nzoom = "prefix+z"', changed)
        self.assertIn('agent_panel_sort = "spaces"', changed)
        self.assertEqual(status.indicator_config(changed), changed)
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib:
            rows = tomllib.loads(changed)['ui']['sidebar']['agents']['rows']
            self.assertEqual(len(rows[0]), 9)
        self.assertEqual(status.colors(original)['crew_working'], '#fe8019')

    def test_styled_native_icon_is_replaced_as_a_complete_token(self):
        original = '[ui.sidebar.agents]\nrows = [[{ token = "state_icon", bold = true }, "agent"]]\n'
        changed = status.indicator_config(original)
        self.assertNotIn('state_icon', changed)
        self.assertNotIn('{ token = {', changed)
        self.assertEqual(changed.count('token = "$herdr_ai_icon_'), 6)
        try:
            import tomllib
        except ImportError:
            return
        self.assertEqual(len(tomllib.loads(changed)['ui']['sidebar']['agents']['rows'][0]), 7)

    def test_observer_reads_completion_sequences_from_session_agents(self):
        live = [pane('agent')]
        live[0]['completion_seq'] = 10
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(status, 'state_dir', return_value=Path(folder)), \
             patch.object(status, 'config_path', return_value=Path(folder) / 'config.toml'), \
             patch.object(sidebar, 'endpoint', return_value='fake.sock'), \
             patch.object(sidebar, 'request', return_value={'snapshot': {'agents': live}}) as call, \
             patch.object(sidebar, 'sync'), \
             patch.object(status, 'install_indicators'), \
             patch.object(status, 'publish') as publish, \
             patch.object(status.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                status.run()
            self.assertEqual(call.call_args.args, ('session.snapshot', {}, 'fake.sock'))
            self.assertEqual(publish.call_args.args[0][0]['completion_seq'], 10)

    def test_config_reload_failure_rolls_back_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.toml'
            original = '[theme]\nname = "gruvbox"\n'
            path.write_text(original)
            call = Mock(side_effect=[{'status': 'failed', 'diagnostics': ['bad layout']}, {'status': 'applied'}])
            with patch.object(status, 'config_path', return_value=path), \
                 patch.object(status, 'state_dir', return_value=Path(folder)):
                with self.assertRaisesRegex(RuntimeError, 'rejected'):
                    status.install_indicators(call)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(call.call_count, 2)

    def test_watcher_lock_allows_one_observer_per_server(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(status, 'state_dir', return_value=Path(folder)):
            with status.watcher_lock('one.sock') as first:
                self.assertTrue(first)
                with status.watcher_lock('one.sock') as second:
                    self.assertFalse(second)
                with status.watcher_lock('other.sock') as other:
                    self.assertTrue(other)
            with status.watcher_lock('one.sock') as next_owner:
                self.assertTrue(next_owner)


if __name__ == '__main__':
    unittest.main()
