from __future__ import annotations
import timeit
from python_extensions import inline_calls, inline_function

@inline_function(register_only=True, shared_region=True)
def heavy(value):
    value = value * 3 + 1
    value = value * 5 - 2
    value = value * 7 + 3
    value = value * 11 - 4
    value = value * 13 + 5
    value = value * 17 - 6
    return value

def raw(value):
    try:
        a=heavy(value); b=heavy(a); c=heavy(b); d=heavy(c)
        return d
    except ArithmeticError:
        return -1

DUP = inline_calls(shared_regions=False, policy='always')(raw)
SHARED = inline_calls(shared_regions='auto', shared_min_body_instructions=1, policy='always')(raw)

def measure(fn, n=200_000):
    for _ in range(10000): fn(2)
    return min(timeit.repeat(lambda:fn(2), number=n, repeat=5))/n*1e9

if __name__ == '__main__':
    expected=raw(2); assert DUP(2)==SHARED(2)==expected
    for name,fn in [('normal',raw),('duplicated',DUP),('protected-shared',SHARED)]:
        print(f'{name:17s} {measure(fn):9.2f} ns  code={len(fn.__code__.co_code):4d} bytes')
    print('duplicated stats:',DUP.__inline_stats__)
    print('shared stats:    ',SHARED.__inline_stats__)
