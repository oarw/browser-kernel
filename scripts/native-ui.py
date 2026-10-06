"""Bounded UI Automation helper for the browser's own Windows controls."""
import argparse
import json
from pywinauto import Desktop

parser = argparse.ArgumentParser()
parser.add_argument('--pid', type=int, required=True)
parser.add_argument('--click')
parser.add_argument('--control', default='Button')
parser.add_argument('--keys')
parser.add_argument('--screenshot')
args = parser.parse_args()
window = Desktop(backend='uia').window(process=args.pid, control_type='Window', found_index=0)
window.wait('exists', timeout=10)
if args.click:
    roots = Desktop(backend='uia').windows(process=args.pid)
    matches = [item for root in roots for item in root.descendants()
               if item.window_text() == args.click and item.element_info.control_type == args.control and item.is_visible()]
    if not matches:
        raise RuntimeError('Requested native control not found: ' + args.click)
    matches[0].invoke()
elif args.keys:
    window.set_focus()
    window.type_keys(args.keys, set_foreground=True)
elif args.screenshot:
    window.capture_as_image().save(args.screenshot)
else:
    print(json.dumps([{'name': item.window_text(), 'type': item.element_info.control_type}
                      for root in Desktop(backend='uia').windows(process=args.pid)
                      for item in root.descendants() if item.is_visible()], ensure_ascii=False))
