"""Unified Path Parsing and Validation Tool"""
import os
from pathlib import Path
from typing import Optional, List

# Default project root directory (can be overridden from environment variables)
DEFAULT_PROJECT_ROOT = os.environ.get(
    'PROJECT_ROOT',
    '/home/senchen/temp/fix_build_agent'
)

# Default whitelist prefixes (relative path format)
DEFAULT_ALLOWED_PREFIXES = [
    'oss-fuzz/projects/',
    'process/project/',
    'generated_prompt_file/',
    'fuzz_build_log_file/',
    'agent_logs/',
    'build_error_log/',
    'solution.txt',
    'reflection_journal.json'
]


def normalize_patch_path(file_path: str, base_dir: Optional[str] = None) -> str:
    """
    Unified patch file path normalization: absolute path → relative path

    Args:
        file_path: Original file path (absolute or relative)
        base_dir: Base directory, uses PROJECT_ROOT by default

    Returns:
        Normalized relative path (using forward slashes)
    """
    if base_dir is None:
        base_dir = DEFAULT_PROJECT_ROOT

    # Handle empty path
    if not file_path:
        return file_path

    # Unify slash style
    file_path = file_path.replace('\\', '/')
    base_dir = base_dir.replace('\\', '/')

    # Convert to absolute paths for comparison
    abs_path = os.path.abspath(file_path)
    abs_base = os.path.abspath(base_dir)

    # Convert to relative path if under the base directory
    if abs_path.startswith(abs_base + os.sep) or abs_path == abs_base:
        rel_path = os.path.relpath(abs_path, abs_base)
        return rel_path.replace('\\', '/')  # Unify output forward slashes

    # Otherwise return the original path (may already be relative)
    return file_path


def validate_patch_path(file_path: str,
                       allowed_prefixes: Optional[List[str]] = None,
                       strict: bool = False) -> bool:
    """
    Whitelist validation: verify if the patch path is within the allowed range

    Args:
        file_path: File path to be validated
        allowed_prefixes: List of allowed path prefixes (relative path format)
        strict: Strict mode, log detailed details on failure

    Returns:
        bool: Whether the path is valid
    """
    if allowed_prefixes is None:
        allowed_prefixes = DEFAULT_ALLOWED_PREFIXES

    # Normalize the path first
    norm_path = normalize_patch_path(file_path)

    # Empty path or current directory is considered valid
    if norm_path in ('', '.', './'):
        return True

    # Check whitelist prefixes
    for prefix in allowed_prefixes:
        prefix_norm = prefix.rstrip('/')
        if norm_path == prefix_norm or norm_path.startswith(prefix_norm + '/'):
            return True

    # Log debug information in strict mode
    if strict:
        from utils.error_handler import format_path_error
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(format_path_error(
            original_path=file_path,
            normalized_path=norm_path,
            base_dir=DEFAULT_PROJECT_ROOT,
            validation_passed=False,
            extra_info={'allowed_prefixes': allowed_prefixes}
        ))

    return False


def ensure_relative_path(file_path: str, base_dir: Optional[str] = None) -> str:
    """
    Force ensure the path is in relative format (used for instruction validation)

    Raises:
        ValueError: When the path cannot be converted to a relative path
    """
    normalized = normalize_patch_path(file_path, base_dir)

    # Check if it is still an absolute path
    if os.path.isabs(normalized):
        raise ValueError(
            f"Path must be relative: '{file_path}' -> '{normalized}'. "
            f"Expected relative to: {base_dir or DEFAULT_PROJECT_ROOT}"
        )

    return normalized
