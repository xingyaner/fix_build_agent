def format_path_error(original_path: str,
                      normalized_path: str,
                      base_dir: str,
                      validation_passed: bool = None,
                      extra_info: dict = None) -> str:
    """
    Formats path-related error information for debugging with standardized PATH GUIDANCE.
    Dynamically renders target directories relative to the current project base.

    Args:
        original_path: The original path provided by the agent
        normalized_path: The path after normalization via normalize_patch_path
        base_dir: The base directory used for relative path resolution
        validation_passed: Whether the path passed whitelist validation (optional)
        extra_info: Additional context for debugging (optional)

    Returns:
        Multi-line formatted error details string with PATH GUIDANCE template
    """
    import json
    import os

    # 🔑 升级：清洗并标准化基准路径，杜绝硬编码提示导致的 LLM 路径偏置幻觉
    clean_base = os.path.abspath(base_dir).replace('\\', '/')

    lines = [
        f"File not found: {original_path}",
        "",
        "【PATH GUIDANCE】",
        "• OSS-Fuzz project configs (Dockerfile, build.sh, project.yaml):",
        f"  → {clean_base}/oss-fuzz/projects/<project_name>/",
        "• Third-party source code:",
        f"  → {clean_base}/process/project/<project_name>/",
        "• Build logs:",
        "  → fuzz_build_log_file/fuzz_build_log.txt",
        "• Generated prompts / commit analysis:",
        "  → generated_prompt_file/",
        "Please verify the absolute path and retry.",
    ]

    # Add technical details for debugging (after the guidance template)
    debug_section = [
        "",
        "【DEBUG INFO】",
        f"  Original:    {original_path}",
        f"  Normalized:  {normalized_path}",
        f"  Base Dir:    {clean_base}",
    ]

    if validation_passed is not None:
        status = "✓ PASS" if validation_passed else "✗ FAIL"
        debug_section.append(f"  Whitelist:   {status}")

    if extra_info:
        debug_section.append("  Context:")
        for key, value in extra_info.items():
            # Format list/dict types with proper indentation
            if isinstance(value, (list, dict)):
                formatted = json.dumps(value, indent=2, ensure_ascii=False)
                for vline in formatted.split('\n'):
                    debug_section.append(f"    {vline}")
            else:
                debug_section.append(f"    {key}: {value}")

    # Combine guidance template + debug info
    return '\n'.join(lines + debug_section)