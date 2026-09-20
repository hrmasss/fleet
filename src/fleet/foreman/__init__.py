"""The foreman — judges whether a session's work is truly done.

It never holds the loop. The queue calls it like a function: one evidence packet in, one
verdict out, process exits. It owns no state, so a crash mid-judgement costs a retry and
nothing else. That single property is what separates this from the background watcher that
died on session restart twice and took the queue with it.
"""

from fleet.foreman.verdicts import RULES, Action, Authority, Rule, Verdict, rule

__all__ = ["RULES", "Action", "Authority", "Rule", "Verdict", "rule"]
