//! 应用共享状态：持久化存储 + 内存缓存（写穿）。

use std::collections::BTreeMap;
use std::sync::Arc;

use rb_format::RoaringSet;
use rb_persist::Store;
use tokio::sync::RwLock;

/// 廉价可克隆的应用状态句柄。
#[derive(Clone)]
pub struct AppState {
    pub store: Store,
    /// 名称 → 缓存集合。用 BTreeMap 让列表天然有序。
    cache: Arc<RwLock<BTreeMap<String, Arc<RoaringSet>>>>,
}

impl AppState {
    pub fn new(store: Store) -> Self {
        AppState {
            store,
            cache: Arc::new(RwLock::new(BTreeMap::new())),
        }
    }

    /// 缓存命中则直接返回，否则从磁盘加载（阻塞 IO 在线程池完成）。
    pub async fn get_cached(&self, name: &str) -> Result<Arc<RoaringSet>, rb_persist::StoreError> {
        if let Some(s) = self.cache.read().await.get(name) {
            return Ok(s.clone());
        }
        let store = self.store.clone();
        let owned = name.to_string();
        let set = tokio::task::spawn_blocking(move || store.load(&owned))
            .await
            .expect("blocking load panicked")?;
        let arc = Arc::new(set);
        self.cache
            .write()
            .await
            .insert(name.to_string(), arc.clone());
        Ok(arc)
    }

    /// 写穿：先原子落盘，再更新缓存。
    pub async fn put(&self, name: &str, set: RoaringSet) -> Result<(), rb_persist::StoreError> {
        let store = self.store.clone();
        let owned = name.to_string();
        let set_for_disk = set.clone();
        tokio::task::spawn_blocking(move || store.save(&owned, &set_for_disk))
            .await
            .expect("blocking save panicked")?;
        self.cache
            .write()
            .await
            .insert(name.to_string(), Arc::new(set));
        Ok(())
    }

    pub async fn evict(&self, name: &str) {
        self.cache.write().await.remove(name);
    }

    pub async fn cache_names(&self) -> Vec<String> {
        self.cache.read().await.keys().cloned().collect()
    }
}
