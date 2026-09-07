#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
模型判断对比工具 - 对比多个 stage02 日志的照片筛选决策差异

功能：
1. 验证多个日志文件是否来自同一输入数据集
2. 解析每个模型对每张照片的判断（保留/删除 + reason）
3. 输出 Excel 报告（完整对比表 + 差异表 + 统计汇总）
4. 提取差异照片到对比目录（可选）
5. Reason 关键词频次分析（可选）

用法：
  python tools/compare_model_decisions.py \
    --logs logs/02_photo_sorter_A.log logs/02_photo_sorter_B.log \
    --output comparison_report \
    [--source-dir D:\测试输入_测试基线_0613] \
    [--extract-photos]
"""

import os
import sys
import re
import argparse
import shutil
from collections import defaultdict, Counter
from datetime import datetime

# Windows 控制台默认 GBK 编码，无法打印 emoji/部分中文，强制切到 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from tabulate import tabulate


def parse_log_metadata(log_path):
    """
    解析日志文件元数据，用于验证是否同一数据集。
    返回: {
        'input_dir': str,
        'provider': str,
        'model': str,
        'extraction_level': str,
        'total_photos': int,  # 提取+剔除合计
        'token_total': int,
        'kept': int,
        'deleted': int,
        'deleted_after_dedup': int
    }
    """
    meta = {}
    with open(log_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 输入目录
    m = re.search(r'\[Input\]\s+(.*?)\s+\[Output\]', content)
    if m:
        meta['input_dir'] = m.group(1).strip()
    
    # Provider 和 Model
    m = re.search(r'\[Provider\]\s+(\w+)\s+\[Model\]\s+(.+)', content)
    if m:
        meta['provider'] = m.group(1).strip()
        meta['model'] = m.group(2).strip()
    
    # 档位
    m = re.search(r'\[提取档位\]\s+([ABC])', content)
    if m:
        meta['extraction_level'] = m.group(1)
    
    # Token 消耗
    m = re.search(r'总 token:\s+([\d,]+)', content)
    if m:
        meta['token_total'] = int(m.group(1).replace(',', ''))
    
    # 02阶段删除/合并去重
    m = re.search(r'02阶段 LLM 剔除:\s+(\d+)\s+张', content)
    if m:
        meta['deleted'] = int(m.group(1))
    
    m = re.search(r'合并去重后总计:\s+(\d+)\s+张', content)
    if m:
        meta['deleted_after_dedup'] = int(m.group(1))
    
    # 统计提取和剔除数量
    kept_count = len(re.findall(r'\[√\] 提取 ->', content))
    deleted_count = len(re.findall(r'\[×\] 剔除 ->', content))
    meta['kept'] = kept_count
    meta['total_photos'] = kept_count + deleted_count
    
    return meta


def parse_log_decisions(log_path):
    """
    解析日志中的照片判断结果。
    返回: {
        'photo_id': {
            'decision': 'keep' | 'delete',
            'reason': str,
            'event_tag': str (仅 keep 有)
        },
        ...
    }
    """
    decisions = {}
    
    with open(log_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            
            # [√] 提取 -> IMG_20260613_095501.jpg | 2026-06-13-公园夏日漫游 | 人物表情自然...
            m = re.search(r'\[√\] 提取 -> ([^\|]+?) \| ([^\|]+?) \| (.+)', line)
            if m:
                photo_id = m.group(1).strip()
                event_tag = m.group(2).strip()
                reason = m.group(3).strip()
                decisions[photo_id] = {
                    'decision': 'keep',
                    'event_tag': event_tag,
                    'reason': reason
                }
                continue
            
            # [×] 剔除 -> IMG_20260613_095502.jpg | 与前一张为连续行走帧...
            m = re.search(r'\[×\] 剔除 -> ([^\|]+?) \| (.+)', line)
            if m:
                photo_id = m.group(1).strip()
                reason = m.group(2).strip()
                decisions[photo_id] = {
                    'decision': 'delete',
                    'event_tag': '',
                    'reason': reason
                }
    
    return decisions


def validate_logs_same_input(metadatas):
    """
    验证所有日志是否来自同一输入数据集。
    如果不一致，返回错误信息列表；一致则返回空列表。
    """
    errors = []
    
    if len(metadatas) < 2:
        errors.append("至少需要2个日志文件进行对比")
        return errors
    
    # 检查输入目录
    input_dirs = [m.get('input_dir') for m in metadatas]
    if len(set(input_dirs)) > 1:
        errors.append(f"输入目录不一致: {set(input_dirs)}")
    
    # 检查总照片数
    total_photos = [m.get('total_photos') for m in metadatas]
    if len(set(total_photos)) > 1:
        errors.append(f"总照片数不一致: {dict(zip([m.get('model') for m in metadatas], total_photos))}")
    
    # 检查档位
    levels = [m.get('extraction_level') for m in metadatas]
    if len(set(levels)) > 1:
        errors.append(f"提取档位不一致: {set(levels)} (警告：不同档位对比意义有限)")
    
    return errors


def build_comparison_table(all_decisions, metadatas):
    """
    构建照片级对比表。
    返回: DataFrame, 包含所有照片在各模型下的判断。
    """
    # 收集所有照片ID
    all_photo_ids = set()
    for decisions in all_decisions.values():
        all_photo_ids.update(decisions.keys())
    
    all_photo_ids = sorted(all_photo_ids)
    
    # 构建行数据
    rows = []
    for photo_id in all_photo_ids:
        row = {'photo_id': photo_id}
        
        decisions_for_photo = []
        for model_key, decisions in all_decisions.items():
            decision_info = decisions.get(photo_id, {'decision': 'MISSING', 'reason': '未在日志中找到', 'event_tag': ''})
            row[f'{model_key}_decision'] = decision_info['decision']
            row[f'{model_key}_reason'] = decision_info['reason']
            row[f'{model_key}_event'] = decision_info.get('event_tag', '')
            decisions_for_photo.append(decision_info['decision'])
        
        # 判断一致性
        unique_decisions = set(d for d in decisions_for_photo if d != 'MISSING')
        if len(unique_decisions) == 1:
            row['consensus'] = '一致'
        elif len(unique_decisions) == 0:
            row['consensus'] = '全MISSING'
        else:
            row['consensus'] = '不一致'
        
        rows.append(row)
    
    df = pd.DataFrame(rows)
    return df


def extract_diff_photos(df, all_decisions, source_dir, output_dir):
    """
    提取判断不一致的照片到对比目录。
    """
    if not source_dir or not os.path.exists(source_dir):
        print(f"⚠️  源目录不存在，跳过照片提取: {source_dir}")
        return
    
    os.makedirs(output_dir, exist_ok=True)
    
    # 获取模型键列表
    model_keys = list(all_decisions.keys())
    
    # 过滤不一致的照片
    diff_df = df[df['consensus'] == '不一致']
    
    if len(diff_df) == 0:
        print("✓ 所有照片判断一致，无需提取")
        return
    
    # 按照差异模式分类
    categories = defaultdict(list)
    
    for _, row in diff_df.iterrows():
        photo_id = row['photo_id']
        decisions = [row[f'{mk}_decision'] for mk in model_keys]
        
        # 判断差异模式
        keep_count = decisions.count('keep')
        delete_count = decisions.count('delete')
        
        if keep_count == 1:
            # 仅一个模型保留
            keeper = model_keys[decisions.index('keep')]
            categories[f'only_{keeper}_kept'].append(photo_id)
        elif delete_count == 1:
            # 仅一个模型删除
            deleter = model_keys[decisions.index('delete')]
            categories[f'only_{deleter}_deleted'].append(photo_id)
        else:
            # 复杂分歧
            categories['complex_disagreement'].append(photo_id)
    
    # 拷贝照片到分类目录
    copied_count = 0
    for category, photo_ids in categories.items():
        cat_dir = os.path.join(output_dir, category)
        os.makedirs(cat_dir, exist_ok=True)
        
        for photo_id in photo_ids:
            # 递归搜索源目录
            src_path = find_photo_in_dir(source_dir, photo_id)
            if src_path:
                dst_path = os.path.join(cat_dir, photo_id)
                shutil.copy2(src_path, dst_path)
                copied_count += 1
    
    print(f"✓ 已提取 {copied_count} 张差异照片到: {output_dir}")
    for cat, ids in categories.items():
        print(f"  - {cat}: {len(ids)} 张")


def find_photo_in_dir(root_dir, photo_id):
    """递归查找照片文件"""
    for dirpath, dirnames, filenames in os.walk(root_dir):
        if photo_id in filenames:
            return os.path.join(dirpath, photo_id)
    return None


def analyze_reason_keywords(all_decisions, metadatas):
    """
    分析各模型 reason 的关键词频次。
    返回: DataFrame, 每个模型的高频词汇统计。
    """
    # 中文分词简化版：按标点和空格切分
    def simple_tokenize(text):
        # 保留中文字词、移除标点
        tokens = re.findall(r'[\u4e00-\u9fff]{2,}', text)  # 2个字以上的中文词
        return tokens
    
    keyword_stats = {}
    
    for model_key, decisions in all_decisions.items():
        keep_reasons = [d['reason'] for d in decisions.values() if d['decision'] == 'keep']
        delete_reasons = [d['reason'] for d in decisions.values() if d['decision'] == 'delete']
        
        keep_tokens = []
        for reason in keep_reasons:
            keep_tokens.extend(simple_tokenize(reason))
        
        delete_tokens = []
        for reason in delete_reasons:
            delete_tokens.extend(simple_tokenize(reason))
        
        keep_counter = Counter(keep_tokens)
        delete_counter = Counter(delete_tokens)
        
        keyword_stats[model_key] = {
            'keep_top10': keep_counter.most_common(10),
            'delete_top10': delete_counter.most_common(10),
            'keep_total_tokens': len(keep_tokens),
            'delete_total_tokens': len(delete_tokens)
        }
    
    # 转为DataFrame便于输出
    rows = []
    for model_key, stats in keyword_stats.items():
        row = {
            'model': model_key,
            'keep_top_keywords': ', '.join([f"{w}({c})" for w, c in stats['keep_top10'][:5]]),
            'delete_top_keywords': ', '.join([f"{w}({c})" for w, c in stats['delete_top10'][:5]]),
            'keep_reason_length': stats['keep_total_tokens'],
            'delete_reason_length': stats['delete_total_tokens']
        }
        rows.append(row)
    
    df = pd.DataFrame(rows)
    return df, keyword_stats


def write_excel_report(df_full, metadatas, all_decisions, output_path, keyword_stats=None):
    """
    输出 Excel 报告，包含多个 sheet。
    """
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        # Sheet 1: 统计汇总
        stats_rows = []
        model_keys = list(all_decisions.keys())
        
        for i, model_key in enumerate(model_keys):
            meta = metadatas[i]
            decisions = all_decisions[model_key]
            kept = sum(1 for d in decisions.values() if d['decision'] == 'keep')
            deleted = len(decisions) - kept
            total = len(decisions)
            keep_rate = kept / total * 100 if total > 0 else 0
            
            stats_rows.append({
                '模型': meta.get('model', 'Unknown'),
                'Provider': meta.get('provider', 'Unknown'),
                '档位': meta.get('extraction_level', 'Unknown'),
                '输入总数': total,
                '保留': kept,
                '删除': deleted,
                '保留率%': round(keep_rate, 1),
                'Token消耗': meta.get('token_total', 0)
            })
        
        df_stats = pd.DataFrame(stats_rows)
        df_stats.to_excel(writer, sheet_name='统计汇总', index=False)
        
        # Sheet 2: 差异统计
        consensus_counts = df_full['consensus'].value_counts()
        diff_rows = [{'类别': k, '数量': v, '占比%': round(v/len(df_full)*100, 1)} for k, v in consensus_counts.items()]
        
        # 详细差异分类
        diff_df = df_full[df_full['consensus'] == '不一致']
        for model_key in model_keys:
            only_keep = sum((diff_df[f'{model_key}_decision'] == 'keep') & 
                           (sum((diff_df[f'{mk}_decision'] == 'delete') for mk in model_keys if mk != model_key) == len(model_keys) - 1))
            if only_keep > 0:
                diff_rows.append({'类别': f'仅{model_key}保留', '数量': only_keep, '占比%': round(only_keep/len(df_full)*100, 1)})
        
        df_diff_stats = pd.DataFrame(diff_rows)
        df_diff_stats.to_excel(writer, sheet_name='差异统计', index=False)
        
        # Sheet 3: 仅差异照片
        diff_cols = ['photo_id', 'consensus'] + [col for col in df_full.columns if col not in ['photo_id', 'consensus']]
        df_diff_only = df_full[df_full['consensus'] == '不一致'][diff_cols]
        df_diff_only.to_excel(writer, sheet_name='差异照片', index=False)
        
        # Sheet 4: 完整对比表
        df_full.to_excel(writer, sheet_name='完整对比', index=False)
        
        # Sheet 5: 关键词分析
        if keyword_stats:
            kw_rows = []
            for model_key, stats in keyword_stats.items():
                kw_rows.append({
                    '模型': model_key,
                    '保留理由高频词TOP5': ', '.join([f"{w}({c})" for w, c in stats['keep_top10'][:5]]),
                    '删除理由高频词TOP5': ', '.join([f"{w}({c})" for w, c in stats['delete_top10'][:5]]),
                    '保留理由总词数': stats['keep_total_tokens'],
                    '删除理由总词数': stats['delete_total_tokens']
                })
            df_keywords = pd.DataFrame(kw_rows)
            df_keywords.to_excel(writer, sheet_name='关键词分析', index=False)
    
    # 美化格式
    wb = load_workbook(output_path)
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        # 表头加粗、背景色
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill(start_color='D9E1F2', end_color='D9E1F2', fill_type='solid')
            cell.alignment = Alignment(horizontal='center', vertical='center')
        
        # 自动列宽
        for column in ws.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if cell.value:
                        max_length = max(max_length, len(str(cell.value)))
                except:
                    pass
            adjusted_width = min(max_length + 2, 80)
            ws.column_dimensions[column_letter].width = adjusted_width
    
    wb.save(output_path)


def main():
    parser = argparse.ArgumentParser(
        description='模型判断对比工具 - 对比多个 stage02 日志的照片筛选决策差异',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python tools/compare_model_decisions.py \\
    --logs logs/02_photo_sorter_20260814_163120.log logs/02_photo_sorter_20260814_165739.log \\
    --output comparison_report \\
    --source-dir D:\\测试输入_测试基线_0613 \\
    --extract-photos
        """
    )
    
    parser.add_argument('--logs', nargs='+', required=True, help='输入的日志文件列表（至少2个）')
    parser.add_argument('--output', required=True, help='输出文件名前缀（不含扩展名）')
    parser.add_argument('--source-dir', help='原始照片所在目录（用于提取差异照片）')
    parser.add_argument('--extract-photos', action='store_true', help='提取判断不一致的照片到对比目录')
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("模型判断对比工具")
    print("=" * 60)
    
    # 1. 解析日志元数据
    print("\n📂 解析日志文件...")
    metadatas = []
    all_decisions = {}
    
    for i, log_path in enumerate(args.logs):
        if not os.path.exists(log_path):
            print(f"❌ 错误: 日志文件不存在: {log_path}")
            return 1
        
        print(f"  [{i+1}] {os.path.basename(log_path)}")
        meta = parse_log_metadata(log_path)
        decisions = parse_log_decisions(log_path)
        
        # 生成唯一键：如果同一模型多次出现，用序号区分（如 ollama-gemma#1, ollama-gemma#2）
        base_key = f"{meta.get('provider', 'unknown')}-{meta.get('model', 'unknown').split('/')[-1][:20]}"
        model_key = base_key
        suffix = 1
        while model_key in all_decisions:
            suffix += 1
            model_key = f"{base_key}#{suffix}"
        
        metadatas.append(meta)
        all_decisions[model_key] = decisions
        
        print(f"      Provider: {meta.get('provider')}, Model: {meta.get('model')}")
        print(f"      档位: {meta.get('extraction_level')}, 总照片: {meta.get('total_photos')}, 保留: {meta.get('kept')}")
    
    # 2. 验证同一输入
    print("\n🔍 验证数据集一致性...")
    errors = validate_logs_same_input(metadatas)
    if errors:
        print("❌ 错误: 日志文件不是同一个输入数据集:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("✓ 验证通过: 所有日志来自同一输入数据集")
    
    # 3. 构建对比表
    print("\n📊 构建照片对比表...")
    df_full = build_comparison_table(all_decisions, metadatas)
    print(f"✓ 共分析 {len(df_full)} 张照片")
    
    consensus_counts = df_full['consensus'].value_counts()
    print(f"  - 一致: {consensus_counts.get('一致', 0)} 张")
    print(f"  - 不一致: {consensus_counts.get('不一致', 0)} 张")
    
    # 4. 关键词分析
    print("\n🔤 分析 reason 关键词...")
    df_keywords, keyword_stats = analyze_reason_keywords(all_decisions, metadatas)
    print("✓ 关键词统计完成")
    
    # 5. 输出 Excel 报告
    output_excel = f"{args.output}.xlsx"
    print(f"\n💾 生成 Excel 报告: {output_excel}")
    write_excel_report(df_full, metadatas, all_decisions, output_excel, keyword_stats)
    print(f"✓ 报告已保存")
    
    # 6. 提取差异照片（可选）
    if args.extract_photos:
        print("\n📸 提取差异照片...")
        extract_dir = f"{args.output}_diff_photos"
        extract_diff_photos(df_full, all_decisions, args.source_dir, extract_dir)
    
    print("\n" + "=" * 60)
    print("✅ 完成")
    print("=" * 60)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
