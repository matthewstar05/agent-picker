"""Shared component selection and launcher definitions for installers."""

import argparse
from pathlib import Path
import sys

MANAGED_MARKER = '# agent-picker managed launcher'

COMMANDS = {
    'agent-picker': ('agent_picker.py', []),
    'agent': ('agent_picker.py', []),
    'usage': ('agent_picker.py', ['--picker-usage']),
    'fs': ('fs_browser.py', []),
    'herdr-fs': ('fs_browser.py', []),
    'star': ('starcli.py', []),
    'starcli': ('starcli.py', []),
    'grok': ('grokbot.py', []),
    'grokbot': ('grokbot.py', []),
    'grokbotcli': ('grokbot.py', []),
    'schedule': ('prompt_scheduler.py', []),
    'herdr-ai': ('herdr_ai.py', []),
    'agent-route': ('agent_route.py', []),
    'herdr-reset': ('herdr_reset.py', []),
}

COMPONENTS = {
    'usage': {'label': 'Usage panel', 'commands': ('usage',)},
    'fs': {'label': 'File browser', 'commands': ('fs', 'herdr-fs')},
    'starcli': {'label': 'StarCLI', 'commands': ('star', 'starcli')},
    'grokbotcli': {'label': 'Grok Bot CLI', 'commands': ('grok', 'grokbot', 'grokbotcli')},
    'schedule': {'label': 'Prompt scheduler', 'commands': ('schedule',)},
    'herdr': {'label': 'Herdr helpers', 'commands': ('herdr-ai', 'agent-route', 'herdr-reset')},
}


def selected_commands(components):
    """Return core launchers and those belonging to the selected components."""
    unknown = set(components) - set(COMPONENTS)
    if unknown:
        raise ValueError('Unknown components: ' + ', '.join(sorted(unknown)))
    names = {'agent-picker', 'agent'}
    for component in components:
        names.update(COMPONENTS[component]['commands'])
    return {name: value for name, value in COMMANDS.items() if name in names}


def _component_list(value):
    if value == 'none':
        return []
    parts = value.split(',')
    if any(not part.strip() for part in parts):
        raise argparse.ArgumentTypeError('component list contains an empty entry')
    parts = [part.strip() for part in parts]
    unknown = set(parts) - set(COMPONENTS)
    if unknown:
        raise argparse.ArgumentTypeError('unknown component(s): ' + ', '.join(sorted(unknown)))
    return parts


def _interactive_selection():
    selected = set(COMPONENTS)
    keys = list(COMPONENTS)
    print('Core commands agent and agent-picker are always installed. Optional selections install command shortcuts; all source remains available through the picker.')
    while True:
        print('\nSelect optional components (toggle numbers, all, none, Enter to install, q to cancel):')
        for index, key in enumerate(keys, 1):
            print('  {}. [{}] {} ({})'.format(index, 'x' if key in selected else ' ', COMPONENTS[key]['label'], key))
        try:
            choice = input('Selection: ').strip().lower()
        except EOFError:
            print('\nInstallation cancelled.')
            raise SystemExit(0)
        if not choice:
            return selected
        if choice == 'q':
            print('Installation cancelled.')
            raise SystemExit(0)
        if choice == 'all':
            selected = set(keys)
        elif choice == 'none':
            selected.clear()
        elif all(part.isdigit() and 1 <= int(part) <= len(keys) for part in choice.split()):
            for part in choice.split():
                key = keys[int(part) - 1]
                if key in selected:
                    selected.remove(key)
                else:
                    selected.add(key)
        else:
            print('Enter a component number, all, none, or q.')


def parse_options(argv=None):
    parser = argparse.ArgumentParser(description='Install agent-picker and optional commands.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--components', type=_component_list, metavar='NAMES', help='comma-separated components, or none')
    group.add_argument('--all', action='store_true', help='select every component')
    parser.add_argument('--without', type=_component_list, metavar='NAMES', help='exclude comma-separated components')
    parser.add_argument('--yes', action='store_true', help='accept defaults without prompting')
    parser.add_argument('--prefix', type=Path, help='installation prefix')
    parser.add_argument('--no-path', action='store_true', help='do not modify shell PATH')
    parser.add_argument('--list-components', action='store_true', help='list optional components and exit')
    options = parser.parse_args(argv)
    if options.list_components:
        for key, item in COMPONENTS.items():
            print('{}: {} ({})'.format(key, item['label'], ', '.join(item['commands'])))
        parser.exit()
    if options.components is not None:
        selected = set(options.components)
    elif options.all or options.without is not None or options.yes or not sys.stdin.isatty():
        selected = set(COMPONENTS)
    else:
        selected = _interactive_selection()
    selected.difference_update(options.without or [])
    options.components = [key for key in COMPONENTS if key in selected]
    print('Selected optional components: ' + (', '.join(options.components) or 'none'))
    return options
