import subprocess
import os
import time

# --- 配置区域 ---
# 定义所有文件和目录的路径，方便管理
LOG_DIR = "fuzz_build_log_file"
LOG_FILE_PATH = os.path.join(LOG_DIR, "fuzz_build_log.txt")

PROMPT_DIR = "generated_prompt_file"
PROMPT_FILE_PATH = os.path.join(PROMPT_DIR, "prompt.txt")

SOLUTION_FILE_PATH = "solution.txt"

# 假设需要分析的项目配置文件和源码位于这两个路径
# 请根据实际情况修改
PROJECT_CONFIG_PATH = "./project_configs" 
PROJECT_SOURCE_PATH = "./project_source"

# --- Agent 脚本文件名 ---
RUN_FUZZ_AGENT = "run_fuzz_and_collect_log_agent.py"
PROMPT_GEN_AGENT = "prompt_generate_agent.py"
SOLVER_AGENT = "fuzzing_solver_agent.py"
APPLIER_AGENT = "solution_applier_agent.py"


def run_agent(command: list):
    """一个辅助函数，用于运行子进程并检查错误。"""
    print(f"Executing: {' '.join(command)}")
    try:
        # 使用 check=True，如果 agent 返回非零退出码（即失败），则会抛出异常
        subprocess.run(command, check=True, text=True, capture_output=True)
        print("Agent executed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"--- AGENT FAILED ---")
        print(f"Command: {' '.join(e.cmd)}")
        print(f"Exit Code: {e.returncode}")
        print(f"Stdout: {e.stdout}")
        print(f"Stderr: {e.stderr}")
        # 抛出异常，终止整个工作流，因为子 agent 失败意味着流程无法继续
        raise

def workflow_loop_agent(project_name: str, max_attempts: int = 5):
    """
    控制整个 Fuzzing 问题解决流程的循环 Agent。

    Args:
        project_name (str): 需要进行 Fuzzing 的项目名称。
        max_attempts (int): 解决问题的最大尝试次数。
    """
    print(f"🚀 Starting Fuzzing Build-Fix Workflow for project: '{project_name}'")
    print(f"Maximum attempts: {max_attempts}")

    # 确保日志和 prompt 目录存在
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(PROMPT_DIR, exist_ok=True)

    for attempt in range(1, max_attempts + 1):
        print(f"\n" + "="*50)
        print(f"🔄 Attempt {attempt}/{max_attempts}")
        print("="*50)

        # --- 步骤 1: 运行 Fuzz 并收集日志 ---
        print("\n[Step 1/5] Running fuzz build and collecting logs...")
        run_agent([
            "python3", RUN_FUZZ_AGENT,
            "--project_name", project_name
        ])

        # --- 步骤 2: 检查构建结果 ---
        print("\n[Step 2/5] Checking build result...")
        try:
            with open(LOG_FILE_PATH, 'r', encoding='utf-8') as f:
                log_content = f.read().strip()
        except FileNotFoundError:
            print(f"❌ Error: Log file '{LOG_FILE_PATH}' not found. Cannot determine build status.")
            break

        if log_content.lower() == "success":
            print("\n" + "🎉"*20)
            print("✅ SUCCESS: Fuzzing build succeeded! The problem has been solved.")
            print("🎉"*20)
            return  # 成功，终止循环
        else:
            print("Build failed. Log content captured. Proceeding to generate a solution.")

        # --- 步骤 3: 生成 Prompt ---
        print("\n[Step 3/5] Generating prompt from logs and project files...")
        run_agent([
            "python3", PROMPT_GEN_AGENT,
            "--tree_path", PROJECT_SOURCE_PATH,
            "--config_path", PROJECT_CONFIG_PATH,
            "--log_path", LOG_FILE_PATH,
            "--output_path", PROMPT_FILE_PATH
        ])
        print(f"Prompt successfully generated at '{PROMPT_FILE_PATH}'")

        # --- 步骤 4: 生成解决方案 ---
        print("\n[Step 4/5] Generating solution from prompt...")
        run_agent([
            "python3", SOLVER_AGENT,
            "--prompt_path", PROMPT_FILE_PATH,
            "--output_path", SOLUTION_FILE_PATH
        ])
        print(f"Solution successfully generated at '{SOLUTION_FILE_PATH}'")

        # --- 步骤 5: 应用解决方案 ---
        print("\n[Step 5/5] Applying the generated solution...")
        run_agent([
            "python3", APPLIER_AGENT,
            "--solution_path", SOLUTION_FILE_PATH
        ])
        print("Solution applied. The next iteration will verify the fix.")
        
        # 为了避免过快循环，可以增加一个短暂的延时
        time.sleep(2)

    # 如果循环完成（达到最大尝试次数）但仍未成功
    print("\n" + "💔"*20)
    print(f"❌ FAILURE: Could not solve the build issue after {max_attempts} attempts.")
    print("💔"*20)


if __name__ == "__main__":
    # 这是一个示例，演示如何驱动 workflow_loop_agent
    # 假设其他 agent 脚本 (run_fuzz_and_collect_log_agent.py 等) 都在同一目录下
    
    # 模拟用户输入项目名称
    target_project = input("Please enter the project name to fuzz and fix: ")
    
    if target_project:
        workflow_loop_agent(target_project)
    else:
        print("No project name provided. Exiting.")
