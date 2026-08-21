                if os.sep in _oc or (os.sep != "/" and "/" in _oc):
                    _oc_enriched = _oc
                else:
                    _oc_enriched = _enrich_call_with_path(
                        _oc,
                        _caller,
                        fallback_class_name=_fallback,
                        caller_method_name=_caller_method,
                        caller_method_param_types=_caller_param_types,
                    )
                _enriched_oc.append(_oc_enriched)
