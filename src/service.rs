//! 服务编排层：索引注册表 + 建索引/加载/查询/校验业务逻辑。
//!
//! 磁盘是唯一真相源（保存以 manifest 原子提交为准）；内存表只是缓存。
//! 重 CPU 的 build/search 由 HTTP 层放到 `spawn_blocking` 执行。

use std::path::{Path, PathBuf};
use std::sync::RwLock;

use crate::config::IndexDefaults;
use crate::error::{FmError, Result};
use crate::fm::FmIndex;
use crate::naive;
use crate::persist::{self, Manifest};

/// 一个已加载索引的内存条目。
pub struct IndexEntry {
    pub index: FmIndex,
    pub manifest: Manifest,
}

/// 索引服务（可在 HTTP handler 间共享）。
pub struct IndexService {
    data_dir: PathBuf,
    import_dirs: Vec<PathBuf>,
    defaults: IndexDefaults,
    max_locations: u64,
    loaded: RwLock<std::collections::BTreeMap<String, IndexEntry>>,
}

impl IndexService {
    pub fn new(
        data_dir: PathBuf,
        import_dirs: Vec<PathBuf>,
        defaults: IndexDefaults,
        max_locations: u64,
    ) -> Self {
        IndexService {
            data_dir,
            import_dirs,
            defaults,
            max_locations,
            loaded: RwLock::new(std::collections::BTreeMap::new()),
        }
    }

    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }
    pub fn defaults(&self) -> &IndexDefaults {
        &self.defaults
    }
    pub fn max_locations(&self) -> u64 {
        self.max_locations
    }

    /// 索引名：1..=64 字符，字母数字 `_-`，禁止路径穿越。
    pub fn validate_name(name: &str) -> Result<()> {
        if name.is_empty() || name.len() > 64 {
            return Err(FmError::invalid_input("index 名长度必须在 1..=64"));
        }
        if !name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        {
            return Err(FmError::invalid_input(
                "index 名只允许 ASCII 字母、数字、'_'、'-'",
            ));
        }
        Ok(())
    }

    /// 启动时自动加载全部磁盘索引；单个加载失败不致命，返回 (成功名, 失败项)。
    pub fn load_all_on_startup(&self) -> (Vec<String>, Vec<(String, FmError)>) {
        let mut ok = Vec::new();
        let mut failed = Vec::new();
        for name in persist::list(&self.data_dir) {
            match self.load(&name) {
                Ok(()) => ok.push(name),
                Err(e) => failed.push((name, e)),
            }
        }
        (ok, failed)
    }

    /// 从磁盘加载（或重载）一个索引到内存。
    pub fn load(&self, name: &str) -> Result<()> {
        Self::validate_name(name)?;
        let (index, manifest) = persist::load(&self.data_dir, name)?;
        let mut map = self
            .loaded
            .write()
            .map_err(|_| FmError::computation_failed("索引注册表锁中毒"))?;
        map.insert(name.to_string(), IndexEntry { index, manifest });
        Ok(())
    }

    /// 是否已加载。
    pub fn is_loaded(&self, name: &str) -> bool {
        self.loaded
            .read()
            .map(|m| m.contains_key(name))
            .unwrap_or(false)
    }

    fn get_loaded(&self, name: &str) -> Result<FmIndex> {
        Self::validate_name(name)?;
        let map = self
            .loaded
            .read()
            .map_err(|_| FmError::computation_failed("索引注册表锁中毒"))?;
        match map.get(name) {
            Some(e) => Ok(e.index.clone()),
            None => {
                if persist::exists(&self.data_dir, name) {
                    Err(FmError::invalid_input(format!(
                        "索引 {name} 在磁盘上但未加载，请先 POST /v1/indexes/{name}/load"
                    )))
                } else {
                    Err(FmError::not_found(format!("索引 {name} 不存在")))
                }
            }
        }
    }

    /// 内存中已加载的索引名（排序）。
    pub fn list_loaded(&self) -> Vec<String> {
        self.loaded
            .read()
            .map(|m| m.keys().cloned().collect())
            .unwrap_or_default()
    }

    /// 磁盘上全部索引名（含未加载）。
    pub fn list_on_disk(&self) -> Vec<String> {
        persist::list(&self.data_dir)
    }

    /// 索引元信息（manifest 优先取自磁盘，反映真相）。
    pub fn describe(&self, name: &str) -> Result<Manifest> {
        Self::validate_name(name)?;
        persist::read_manifest(&self.data_dir, name)
    }

    /// 从内存文本构建并持久化（磁盘已存在同名索引则 state_conflict）。
    pub fn create_from_text(
        &self,
        name: &str,
        text: Vec<u8>,
        rank_block: Option<u32>,
        sample_step: Option<u32>,
    ) -> Result<Manifest> {
        Self::validate_name(name)?;
        if text.is_empty() {
            return Err(FmError::invalid_input("text 不能为空"));
        }
        if text.len() as u64 > self.defaults.max_text_bytes {
            return Err(FmError::resource_exhausted(format!(
                "文本 {} 字节超过上限 {} 字节",
                text.len(),
                self.defaults.max_text_bytes
            )));
        }
        // 提前对冲突做显式判断（persist::save 也会再判一次）。
        if persist::exists(&self.data_dir, name) {
            return Err(FmError::state_conflict(format!("索引 {name} 已存在")));
        }
        let rb = rank_block.unwrap_or(self.defaults.rank_block);
        let ss = sample_step.unwrap_or(self.defaults.sample_step);
        let index = FmIndex::build(&text, rb, ss)?;
        let manifest = persist::save(&self.data_dir, name, &index)?;
        let mut map = self
            .loaded
            .write()
            .map_err(|_| FmError::computation_failed("索引注册表锁中毒"))?;
        map.insert(
            name.to_string(),
            IndexEntry {
                index,
                manifest: manifest.clone(),
            },
        );
        Ok(manifest)
    }

    /// 从本地白名单文件导入文本并建索引。
    pub fn create_from_file(
        &self,
        name: &str,
        rel_path: &str,
        rank_block: Option<u32>,
        sample_step: Option<u32>,
    ) -> Result<Manifest> {
        Self::validate_name(name)?;
        let full = self.resolve_import_path(rel_path)?;
        let text = std::fs::read(&full)?;
        self.create_from_text(name, text, rank_block, sample_step)
    }

    /// 把请求中的相对路径解析到 import_dirs 白名单内，拒绝任何逃逸。
    fn resolve_import_path(&self, rel: &str) -> Result<PathBuf> {
        if rel.is_empty() {
            return Err(FmError::invalid_input("path 不能为空"));
        }
        let p = Path::new(rel);
        if p.is_absolute() {
            return Err(FmError::invalid_input(
                "path 必须是相对路径（相对白名单目录）",
            ));
        }
        if rel.contains('\0') {
            return Err(FmError::invalid_input("path 含非法空字节"));
        }
        let mut candidates: Vec<PathBuf> = Vec::new();
        for base in &self.import_dirs {
            let joined = base.join(p);
            if let Ok(canon) = joined.canonicalize()
                && let Ok(base_canon) = base.canonicalize()
                && canon.starts_with(&base_canon)
            {
                candidates.push(canon);
            }
        }
        candidates.into_iter().find(|c| c.is_file()).ok_or_else(|| {
            FmError::not_found(format!(
                "文件 {rel} 不在 import_dirs 白名单内或不存在（白名单: {:?}）",
                self.import_dirs
            ))
        })
    }

    /// 查询：返回 (count, locations, trace_steps)。trace=false 时 steps 为空。
    pub fn search(&self, name: &str, pattern: &[u8], trace: bool) -> Result<SearchResult> {
        let index = self.get_loaded(name)?;
        let (iv, steps) = index.search_traced(pattern, trace);
        // 空模式与命中区间都需要位置；先按 max_locations 做资源护栏。
        if iv.count() > self.max_locations {
            return Err(FmError::resource_exhausted(format!(
                "命中数 {} 超过单次定位上限 {}（可用更具体的模式或缩小查询）",
                iv.count(),
                self.max_locations
            )));
        }
        let locations = if trace {
            // trace 模式只返回区间与步骤，不强制展开全部位置（仍受护栏保护）。
            index.locate(pattern)?
        } else {
            index.locate(pattern)?
        };
        Ok(SearchResult {
            count: iv.count(),
            interval_lo: iv.lo,
            interval_hi: iv.hi,
            locations,
            steps,
        })
    }

    /// 仅计数（不定位，避免大量位置展开）。
    pub fn count(&self, name: &str, pattern: &[u8]) -> Result<u64> {
        let index = self.get_loaded(name)?;
        Ok(index.count(pattern))
    }

    /// 验证接口：FM 结果与独立朴素扫描逐项对照。
    pub fn verify(&self, name: &str, pattern: &[u8]) -> Result<VerifyResult> {
        let index = self.get_loaded(name)?;
        let fm_count = index.count(pattern);
        if fm_count > self.max_locations {
            return Err(FmError::resource_exhausted(format!(
                "命中数 {fm_count} 超过定位上限 {}",
                self.max_locations
            )));
        }
        let fm_locations = index.locate(pattern)?;
        let naive_count = naive::naive_count(index.text(), pattern);
        let naive_locations = naive::naive_locate(index.text(), pattern);
        let verdict = naive::compare(index.text(), fm_count, &fm_locations, pattern);
        Ok(VerifyResult {
            fm_count,
            naive_count,
            fm_locations,
            naive_locations,
            agree: verdict == naive::Verdict::Agree,
            detail: match verdict {
                naive::Verdict::Agree => "count 与 locations 均与朴素扫描一致".to_string(),
                naive::Verdict::CountMismatch { fm, naive } => {
                    format!("count 不一致: fm={fm} naive={naive}")
                }
                naive::Verdict::LocationsMismatch { detail } => {
                    format!("locations 不一致: {detail}")
                }
            },
        })
    }

    /// 删除磁盘索引并从内存摘除。
    pub fn delete(&self, name: &str) -> Result<()> {
        Self::validate_name(name)?;
        persist::remove(&self.data_dir, name)?;
        if let Ok(mut map) = self.loaded.write() {
            map.remove(name);
        }
        Ok(())
    }
}

/// 查询结果。
#[derive(Debug, Clone, serde::Serialize)]
pub struct SearchResult {
    pub count: u64,
    /// 后向搜索半开区间 [lo, hi)。
    pub interval_lo: u64,
    pub interval_hi: u64,
    pub locations: Vec<u64>,
    pub steps: Vec<crate::fm::SearchStep>,
}

/// verify 对照结果。
#[derive(Debug, Clone, serde::Serialize)]
pub struct VerifyResult {
    pub fm_count: u64,
    pub naive_count: u64,
    pub fm_locations: Vec<u64>,
    pub naive_locations: Vec<u64>,
    pub agree: bool,
    pub detail: String,
}
