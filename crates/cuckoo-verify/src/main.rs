//! 独立验证器入口。
//!
//! 用法：
//! - `cuckoo-verify`            运行全部场景，文本日志输出到 stdout
//! - `cuckoo-verify --json DIR`  额外把每场景 JSON 报告写入 DIR
//! - `cuckoo-verify --run-id ID` 指定可关联的运行身份
//!
//! 退出码：全部场景 0；任一失败 2（CI 可据此判定）。

use std::fs;
use std::path::PathBuf;

use cuckoo_verify::harness::new_run_id;
use cuckoo_verify::scenarios::run_all;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut json_dir: Option<PathBuf> = None;
    let mut run_id: Option<String> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--json" => {
                i += 1;
                json_dir = args.get(i).map(PathBuf::from);
            }
            "--run-id" => {
                i += 1;
                run_id = args.get(i).cloned();
            }
            other => {
                eprintln!("未知参数: {other}（支持 --json DIR / --run-id ID）");
                std::process::exit(64);
            }
        }
        i += 1;
    }
    let run_id = run_id.unwrap_or_else(new_run_id);

    if let Some(dir) = &json_dir {
        if let Err(e) = fs::create_dir_all(dir) {
            eprintln!("创建报告目录 {} 失败: {e}", dir.display());
            std::process::exit(64);
        }
    }

    println!("运行身份: {run_id}");
    println!(
        "内核版本 {} / 存储格式 v{}",
        cuckoo_core::CORE_VERSION,
        cuckoo_core::FORMAT_VERSION
    );

    let reports = run_all(Some(run_id.clone()));
    let mut any_fail = false;
    for rep in &reports {
        println!("{}", rep.render_text());
        if let Some(dir) = &json_dir {
            let path = dir.join(format!("{}-{}.json", rep.id, run_id));
            if let Err(e) = fs::write(&path, rep.render_json()) {
                eprintln!("写报告 {} 失败: {e}", path.display());
                std::process::exit(64);
            }
            println!("JSON 报告: {}", path.display());
        }
        if !rep.passed() {
            any_fail = true;
        }
    }

    let total_steps: usize = reports.iter().map(|r| r.steps.len()).sum();
    let failed_steps: usize = reports
        .iter()
        .map(|r| r.steps.iter().filter(|s| !s.verdict.is_pass()).count())
        .sum();
    println!("==================================================");
    println!(
        "汇总: 场景 {}/{} 通过，步骤 {}/{} 通过（run={}）",
        reports.iter().filter(|r| r.passed()).count(),
        reports.len(),
        total_steps - failed_steps,
        total_steps,
        run_id
    );

    if any_fail {
        std::process::exit(2);
    }
}
