OUT_BASE = os.path.splitext(INPUT_PATH)[0]
map_df_export.to_csv(             f"{OUT_BASE}_Parent_to_Chunks.csv",    index=False)
child_graph_reduced_df.to_csv(    f"{OUT_BASE}_Child_Graph_Reduced.csv",  index=False)
reusability_edges_df.to_csv(      f"{OUT_BASE}_Reusability_Edges.csv",    index=False)
reusability_summary_df.to_csv(    f"{OUT_BASE}_Reusability_Summary.csv",  index=False)
execution_order_export_df.to_csv( f"{OUT_BASE}_Execution_Order.csv",      index=False)
hierarchy_df.to_csv(              f"{OUT_BASE}_Hierarchy_JSON.csv",       index=False)
chunk_methods_long_df.to_csv(     f"{OUT_BASE}_Chunk_Methods_Long.csv",   index=False)

print(f"\n[DONE] Exported CSVs to: {OUT_BASE}_*.csv")
return f"{OUT_BASE}_Parent_to_Chunks.csv", program_or_process, CHUNK_LIMIT
