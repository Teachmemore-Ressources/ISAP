"""
ISAP — test_hlc.py
Tests de propriété sur Hybrid Logical Clock (SPEC.md §5).

Vérifie :
1. Monotonie locale : HLC.send() retourne toujours un timestamp ≥ précédent.
2. Préservation causale : après receive(msg), HLC > HLC_msg.
3. Comparaison ISAP-HLC-1 : (l_a, c_a) < (l_b, c_b) ssi a précède b.
4. Robustesse à la dérive d'horloge (pas de régression du compteur).

Pas besoin de pytest ; lance simplement `python test_hlc.py`.
"""
import random
import sys
import time
from typing import Tuple

# Reproduit la classe HLC du agent — version standalone pour test
class HLC:
    def __init__(self, clock_offset_ms: int = 0):
        self._offset = clock_offset_ms
        self.l = self._now()
        self.c = 0

    def _now(self) -> int:
        return int(time.time() * 1000) + self._offset

    def send(self):
        t = self._now()
        l_new = max(self.l, t)
        if l_new == self.l:
            self.c += 1
        else:
            self.l = l_new
            self.c = 0
        return {"l": self.l, "c": self.c}

    def receive(self, msg_l: int, msg_c: int):
        t = self._now()
        l_new = max(self.l, msg_l, t)
        if l_new == self.l == msg_l:
            self.c = max(self.c, msg_c) + 1
        elif l_new == self.l:
            self.c += 1
        elif l_new == msg_l:
            self.c = msg_c + 1
        else:
            self.c = 0
        self.l = l_new
        return {"l": self.l, "c": self.c}


def hlc_lt(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    """Comparaison causale : (l_a, c_a) < (l_b, c_b)."""
    return a[0] < b[0] or (a[0] == b[0] and a[1] < b[1])


def to_tuple(h):
    return (h["l"], h["c"])


# ──────────────────────────────────────────────
# Test 1 — Monotonie sur send()
# ──────────────────────────────────────────────
def test_monotonic_send(n_calls: int = 1000):
    h = HLC()
    prev = (0, 0)
    for _ in range(n_calls):
        cur = to_tuple(h.send())
        assert hlc_lt(prev, cur), f"non-monotonic: {prev} → {cur}"
        prev = cur
    return True


# ──────────────────────────────────────────────
# Test 2 — Préservation causale après réception
# ──────────────────────────────────────────────
def test_causal_after_recv(n_pairs: int = 500):
    a = HLC()
    b = HLC()
    for _ in range(n_pairs):
        msg = a.send()
        recv = b.receive(msg["l"], msg["c"])
        assert hlc_lt(to_tuple(msg), to_tuple(recv)), \
            f"causality broken: msg={msg} recv={recv}"
    return True


# ──────────────────────────────────────────────
# Test 3 — Robustesse au clock skew (NTP drift simulé)
# ──────────────────────────────────────────────
def test_clock_skew(skew_ms: int = -300):
    """Un nœud avec horloge en retard reçoit d'un nœud en avance.
    HLC doit toujours préserver l'ordre causal indépendamment du skew."""
    a = HLC(clock_offset_ms=0)            # référence
    b = HLC(clock_offset_ms=skew_ms)      # 300ms de retard NTP
    for _ in range(200):
        msg = a.send()
        recv = b.receive(msg["l"], msg["c"])
        assert hlc_lt(to_tuple(msg), to_tuple(recv)), \
            f"skew broke causality: msg={msg} recv={recv}"
    return True


# ──────────────────────────────────────────────
# Test 4 — Comparaison antisymétrique
# ──────────────────────────────────────────────
def test_antisymmetric():
    pairs = [
        ((100, 0), (100, 1)),
        ((100, 5), (101, 0)),
        ((1714, 99), (1715, 0)),
    ]
    for a, b in pairs:
        assert hlc_lt(a, b), f"{a} should < {b}"
        assert not hlc_lt(b, a), f"{b} should not < {a}"
    # égalité
    assert not hlc_lt((100, 5), (100, 5))
    return True


# ──────────────────────────────────────────────
# Test 5 — Convergence après échanges multi-directionnels
# ──────────────────────────────────────────────
def test_bidirectional_convergence(n_rounds: int = 100):
    a, b, c = HLC(), HLC(), HLC()
    for _ in range(n_rounds):
        # A → B → C → A en chaîne
        m1 = a.send()
        m2 = b.receive(m1["l"], m1["c"])
        m3 = c.receive(m2["l"], m2["c"])
        m4 = a.receive(m3["l"], m3["c"])
        # le HLC final de a doit être ≥ le m1 de départ
        assert hlc_lt(to_tuple(m1), to_tuple(m4)) or to_tuple(m1) == to_tuple(m4), \
            f"chain regression: m1={m1} m4={m4}"
    return True


# ──────────────────────────────────────────────
# RUN ALL
# ──────────────────────────────────────────────
TESTS = [
    ("monotonic_send",            test_monotonic_send),
    ("causal_after_recv",         test_causal_after_recv),
    ("clock_skew_ntp_drift",      test_clock_skew),
    ("antisymmetric_comparison",  test_antisymmetric),
    ("bidirectional_chain",       test_bidirectional_convergence),
]

def main():
    ok = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  OK  test_{name}")
            ok += 1
        except AssertionError as e:
            print(f"  FAIL  test_{name} — {e}")
        except Exception as e:
            print(f"  ERR   test_{name} — {type(e).__name__}: {e}")

    print(f"\n{ok}/{len(TESTS)} HLC property tests passed")
    sys.exit(0 if ok == len(TESTS) else 1)


if __name__ == "__main__":
    main()
