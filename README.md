# After: chunks_df = pd.DataFrame(final_unique)

txt_out = os.path.splitext(INPUT_PATH)[0] + "_methods_in_chunk.txt"
with open(txt_out, "w", encoding="utf-8") as f:
    for _, row in chunks_df.iterrows():
        f.write(f"=== {row['chunk_id']} ===\n")
        f.write(f"{row.get('methods_in_chunk', '')}\n\n")
print(f"[DONE] Methods written to: {txt_out}")
