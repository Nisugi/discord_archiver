#!/usr/bin/env python3
"""
Check for mismatches between SEED_BLUE_IDS and GM_NAME_OVERRIDES
"""
import re
import sys
from pathlib import Path

# Read config file directly to avoid import errors
config_path = Path(__file__).parent.parent / "bot" / "archiver" / "config.py"
with open(config_path) as f:
    config_text = f.read()

# Extract SEED_BLUE_IDS
# First check if it's the new refactored pattern
if 'SEED_BLUE_IDS = set(GM_NAME_OVERRIDES.keys())' in config_text:
    # New pattern: SEED_BLUE_IDS is derived from GM_NAME_OVERRIDES
    # We'll verify they match later
    seed_ids = None  # Will be populated from GM_NAME_OVERRIDES
    refactored = True
else:
    # Old pattern: SEED_BLUE_IDS is a literal set
    seed_match = re.search(r'SEED_BLUE_IDS = \{([^}]+)\}', config_text, re.DOTALL)
    seed_ids = set()
    refactored = False
    if seed_match:
        for line in seed_match.group(1).split('\n'):
            match = re.search(r'(\d+)', line)
            if match:
                seed_ids.add(int(match.group(1)))

# Extract GM_NAME_OVERRIDES
override_match = re.search(r'GM_NAME_OVERRIDES = \{([^}]+)\}', config_text, re.DOTALL)
override_ids = {}
if override_match:
    for line in override_match.group(1).split('\n'):
        match = re.search(r'(\d+):\s*"([^"]+)"', line)
        if match:
            override_ids[int(match.group(1))] = match.group(2)

override_ids_set = set(override_ids.keys())

# If refactored, SEED_BLUE_IDS should be identical to GM_NAME_OVERRIDES.keys()
if refactored:
    seed_ids_set = override_ids_set  # They should be the same
    missing_from_seed = set()
    missing_from_override = set()
else:
    seed_ids_set = seed_ids
    # Find IDs in GM_NAME_OVERRIDES but NOT in SEED_BLUE_IDS
    missing_from_seed = override_ids_set - seed_ids_set
    # Find IDs in SEED_BLUE_IDS but NOT in GM_NAME_OVERRIDES
    missing_from_override = seed_ids_set - override_ids_set

print('=' * 70)
print('GM List Consistency Check')
print('=' * 70)
print()

if refactored:
    print('PATTERN: Refactored (SEED_BLUE_IDS derived from GM_NAME_OVERRIDES)')
    print('This is the recommended pattern - single source of truth!')
else:
    print('PATTERN: Legacy (SEED_BLUE_IDS and GM_NAME_OVERRIDES separate)')
    print('Consider refactoring to: SEED_BLUE_IDS = set(GM_NAME_OVERRIDES.keys())')
print()

print(f'SEED_BLUE_IDS count: {len(seed_ids_set)}')
print(f'GM_NAME_OVERRIDES count: {len(override_ids_set)}')
print()

print('IDs in GM_NAME_OVERRIDES but NOT in SEED_BLUE_IDS:')
print('(These GMs won\'t be marked as GMs in the database!)')
print('-' * 70)
if missing_from_seed:
    print(f'WARNING: Found {len(missing_from_seed)} IDs that need to be added to SEED_BLUE_IDS:')
    print()
    for gm_id in sorted(missing_from_seed):
        print(f'    {gm_id},  # {override_ids[gm_id]}')
else:
    print('OK: All override IDs are in SEED_BLUE_IDS')
print()

print('IDs in SEED_BLUE_IDS but NOT in GM_NAME_OVERRIDES:')
print('(These GMs will use their Discord display name)')
print('-' * 70)
if missing_from_override:
    print(f'INFO: {len(missing_from_override)} IDs (this is OK if they use their Discord name)')
else:
    print('None - all seed IDs have name overrides')
print()

if missing_from_seed:
    print('=' * 70)
    print('ACTION REQUIRED!')
    print('=' * 70)
    if refactored:
        print('ERROR: This should not happen with the refactored pattern!')
        print('Check that SEED_BLUE_IDS = set(GM_NAME_OVERRIDES.keys()) is correct.')
    else:
        print('Add the missing IDs above to SEED_BLUE_IDS in:')
        print('  - bot/archiver/config.py')
        print('  - source/archiver/config.py')
        print()
        print('OR refactor to use: SEED_BLUE_IDS = set(GM_NAME_OVERRIDES.keys())')
    print()
    sys.exit(1)
else:
    if refactored:
        print('OK: Using refactored pattern - SEED_BLUE_IDS automatically synced with GM_NAME_OVERRIDES')
    else:
        print('OK: All GM_NAME_OVERRIDES IDs are in SEED_BLUE_IDS')
    sys.exit(0)
