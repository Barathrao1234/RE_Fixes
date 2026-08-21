            # If class_method_call already contains a path separator it has already
            # been resolved by derive_chain_segments / BFS — re-enriching it would
            # corrupt the resolved path (the path's first segment gets mistaken for
            # a variable name).  Skip enrichment and keep it as-is.
            if os.sep in _cmc or (os.sep != "/" and "/" in _cmc):
                _cmc_final = _cmc
            else:
                _cmc_enriched = _enrich_call_with_path(
                    _cmc,
                    _caller,
                    caller_method_name=_caller_method,
                    caller_method_param_types=_caller_param_types,
                    source_object_call=_src_oc,
                )
                _cmc_final = _cmc_enriched if _cmc_enriched is not None else _cmc
