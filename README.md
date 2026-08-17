resolved = _resolve_class_path(mapped_cls, caller_file, member_name=member_name)
            _dbg_file = os.path.basename(str(caller_file or ""))
            if "CreateService" in _dbg_file:
                print(f"[ENRICH] cls_name={cls_name!r} mapped_cls={mapped_cls!r}")
                print(f"[ENRICH] resolved={resolved!r}")
            if resolved:
                _result = "{}.{}".format(_strip_extension(resolved), rest)
                if "CreateService" in _dbg_file:
                    print(f"[ENRICH] OUT {_result!r}")
                return _result
            _fallback = "{}.{}".format(mapped_cls, rest)
            if "CreateService" in _dbg_file:
                print(f"[ENRICH] FALLBACK {_fallback!r}")
            return _fallback
