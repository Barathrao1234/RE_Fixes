if _dep:
                    _debug_calls = {"req.details", "res.post"}   # ← method names to watch (no parens)
                    if any(_call.startswith(d) for d in _debug_calls):
                        print(f"[BFS] {_call}  ->  {_dep}")
                    _enqueue(_dep)
