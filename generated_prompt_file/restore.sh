TARGET_FILE="projects.yaml"
BACKUP_FILE="${TARGET_FILE}.bak"

# 检查目标文件是否存在
if [ ! -f "$TARGET_FILE" ]; then
    echo "错误: 文件 '$TARGET_FILE' 不存在！"
    exit 1
fi
sed -i.bak -e '/fix_result: Failure/d' \
           -e '/fix_date:/d' \
           -e "s/state: 'yes'/state: 'no'/" \
           "$TARGET_FILE"

# 检查操作是否成功
if [ $? -eq 0 ]; then
    echo "✅ 文件清理完成！"
    echo ""
    echo "修改摘要："
    echo "  - 已删除所有 'fix_result: Failure' 行"
    echo "  - 已删除所有 'fix_date:' 行"
    echo "  - 已将所有 'state: \"yes\"' 改为 'state: \"no\"'"
else
    echo "❌ 处理文件时出错！"
    exit 1
fi

