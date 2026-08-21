        for _impl in _impl_candidates:
            if _impl in _visited:
                continue
            _visited.add(_impl)
            _impl_owner = _walk_extends(_impl)
            if _impl_owner:
                return _impl_owner

        # Also check concrete subclasses that extend this class (abstract base pattern):
        # classA extends ClassB → if ClassB.build is called, prefer ClassA.build
        for _sub in _concrete_subclasses.get(_start, []):
            if _sub in _visited:
                continue
            _visited.add(_sub)
            if _class_declares_method(_sub, _method_name):
                return _sub

        return _start
