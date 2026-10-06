import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from package_candidate import patterns_from_cfg, runtime_members, PREFIX


class CandidatePackageTests(unittest.TestCase):
    def test_only_runtime_files_and_component_dlls(self):
        files = ['chrome.exe', 'chrome.dll', 'icudtl.dat', 'resources.pak', 'chrome_100_percent.pak',
                 'locales/en-US.pak', 'base.dll', 'chrome.exe.pdb', 'obj/secrets.obj', 'setup.exe']
        selected = dict(runtime_members([PREFIX + name for name in files], ['chrome.exe', 'icudtl.dat', '*.pak', 'locales', '*.pdb', 'setup.exe']))
        self.assertIn(PREFIX + 'base.dll', selected)
        self.assertNotIn(PREFIX + 'chrome.exe.pdb', selected)
        self.assertNotIn(PREFIX + 'obj/secrets.obj', selected)
        self.assertNotIn(PREFIX + 'setup.exe', selected)

    def test_missing_runtime_dependency_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'Missing runtime'):
            runtime_members([PREFIX + 'chrome.exe'], ['chrome.exe'])

    def test_cfg_is_literal_and_arch_filtered(self):
        self.assertEqual(patterns_from_cfg("FILES = [{'filename':'chrome.exe','buildtype':['official'],'arch':['64bit']}, {'filename':'x86.dll','buildtype':['official'],'arch':['32bit']} ]"), ['chrome.exe'])
        with self.assertRaises((ValueError, RuntimeError)):
            patterns_from_cfg("FILES = __import__('os').listdir('.')")

    def test_unsafe_path_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Invalid relative'):
            runtime_members([PREFIX + '../chrome.exe'], ['*'])
