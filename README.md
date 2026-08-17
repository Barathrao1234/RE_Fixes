if "YourFile" in os.path.basename(str(caller_file or "")):
                print(f"[ENRICH] cls_name={cls_name!r}")
                print(f"[ENRICH] var_map.get={var_map.get(cls_name)!r}")
                print(f"[ENRICH] ocm_scoped={object_class_map.get((_ocm_scoped_key, cls_name.lower()))!r}")
                print(f"[ENRICH] ocm_global={object_class_map.get(cls_name.lower())!r}")
                print(f"[ENRICH] mapped_cls={mapped_cls!r}")
