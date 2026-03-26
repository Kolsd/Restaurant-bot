#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.run_ai_simulations as sim

FAILING_IDS = {12, 23, 32, 34, 35}
targets = [s for s in sim.SCENARIOS if s.id in FAILING_IDS]

print(f"Re-checking {len(targets)} previously failed scenarios\n")
print("=" * 62)

all_passed = True
for s in targets:
    r = sim.run_scenario(s)
    icon  = "PASS" if r.passed else "FAIL"
    score = r.judgment.get("score", "?")
    action = (r.bot_parsed or {}).get("action", "N/A")
    reply  = (r.bot_parsed or {}).get("reply", "")[:100]
    rules  = r.judgment.get("violated_rules", [])
    expl   = r.judgment.get("explanation", "")
    print(f"[{icon}] #{s.id}: {s.name}")
    print(f"       Action={action} | Score={score}/100")
    print(f"       Reply: {reply}...")
    if not r.passed:
        all_passed = False
        print(f"  !! STILL FAILING: {expl}")
        if rules:
            print(f"     Rules: {', '.join(rules)}")
    print()

print("=" * 62)
print("VERDICT:", "ALL 5 FIXED (50/50 EXPECTED)" if all_passed else "SOME STILL FAILING")
sys.exit(0 if all_passed else 1)
