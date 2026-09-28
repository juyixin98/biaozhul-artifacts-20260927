//! 集成测试公共层：独立于内核的参考状态机（穷举）与夹具装载。
//!
//! 参考实现有意用最简单、与生产代码风格完全不同的写法（递归 + BTreeSet 元组集合），
//! 独立计算可达集合，用来对照内核 BFS 与 API 结论；它不 import 任何求解函数。
#![allow(dead_code)]

use std::collections::{BTreeMap, BTreeSet};

pub fn load_fixture(name: &str) -> serde_json::Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("fixtures")
        .join(name);
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {path:?}: {e}"));
    serde_json::from_str(&text).expect("fixture must be valid JSON")
}

#[derive(Debug, Clone)]
pub struct RefNet {
    pub place_names: Vec<String>,
    pub capacities: Vec<i64>,
    pub transition_names: Vec<String>,
    pub inputs: Vec<Vec<(usize, i64)>>,
    pub outputs: Vec<Vec<(usize, i64)>>,
}

impl RefNet {
    /// 从夹具 JSON 直接构造（不经内核/输入层）。
    pub fn from_fixture(v: &serde_json::Value) -> Self {
        let place_names: Vec<String> = v["places"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p["name"].as_str().unwrap().to_string())
            .collect();
        let capacities: Vec<i64> = v["places"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p["capacity"].as_i64().unwrap())
            .collect();
        let pidx: BTreeMap<String, usize> = place_names
            .iter()
            .enumerate()
            .map(|(i, n)| (n.clone(), i))
            .collect();
        let mut transition_names = Vec::new();
        let mut inputs = Vec::new();
        let mut outputs = Vec::new();
        for t in v["transitions"].as_array().unwrap() {
            transition_names.push(t["name"].as_str().unwrap().to_string());
            let mk = |key: &str| -> Vec<(usize, i64)> {
                t[key]
                    .as_array()
                    .map(|arcs| {
                        arcs.iter()
                            .map(|a| {
                                (
                                    pidx[a["place"].as_str().unwrap()],
                                    a["weight"].as_i64().unwrap_or(1),
                                )
                            })
                            .collect()
                    })
                    .unwrap_or_default()
            };
            inputs.push(mk("inputs"));
            outputs.push(mk("outputs"));
        }
        RefNet {
            place_names,
            capacities,
            transition_names,
            inputs,
            outputs,
        }
    }

    pub fn initial_from_fixture(v: &serde_json::Value) -> Vec<i64> {
        v["initial_marking"]
            .as_array()
            .map(|a| a.iter().map(|x| x.as_i64().unwrap_or(0)).collect())
            .unwrap_or_else(|| vec![0; v["places"].as_array().unwrap().len()])
    }

    /// 初始标识由 name->count 稀疏映射给出（模拟 API 请求）。
    pub fn initial_from_map(&self, map: &BTreeMap<String, i64>) -> Vec<i64> {
        let mut m = vec![0i64; self.place_names.len()];
        for (name, n) in map {
            let i = self
                .place_names
                .iter()
                .position(|p| p == name)
                .unwrap_or_else(|| panic!("unknown place {name}"));
            m[i] = *n;
        }
        m
    }

    /// 参考发射语义：同时检查全部输入弧 + 容量原子前提；通过则原子消耗/生成。
    pub fn ref_fire(&self, m: &[i64], ti: usize) -> Option<Vec<i64>> {
        for &(p, w) in &self.inputs[ti] {
            if m[p] < w {
                return None;
            }
        }
        let mut next = m.to_vec();
        for &(p, w) in &self.inputs[ti] {
            next[p] -= w;
        }
        for &(p, w) in &self.outputs[ti] {
            next[p] += w;
        }
        for (p, tokens) in next.iter().enumerate() {
            if *tokens > self.capacities[p] {
                return None;
            }
            assert!(*tokens >= 0, "tokens never negative after legal consume");
        }
        Some(next)
    }

    /// 穷举可达集合（容量空间有限）。返回 (可达集合, 父边映射)。
    pub fn exhaustive_reachable(&self, initial: &[i64]) -> BTreeSet<Vec<i64>> {
        let mut seen: BTreeSet<Vec<i64>> = BTreeSet::new();
        let mut stack = vec![initial.to_vec()];
        seen.insert(initial.to_vec());
        while let Some(m) = stack.pop() {
            for ti in 0..self.transition_names.len() {
                if let Some(n) = self.ref_fire(&m, ti) {
                    if seen.insert(n.clone()) {
                        stack.push(n);
                    }
                }
            }
        }
        seen
    }

    /// 从可达集合中找一条最短合法序列（参考 BFS），返回变迁名。
    pub fn ref_path(&self, initial: &[i64], target: &[i64]) -> Option<Vec<String>> {
        use std::collections::{HashMap, VecDeque};
        if initial == target {
            return Some(Vec::new());
        }
        let mut parent: HashMap<Vec<i64>, (Vec<i64>, usize)> = HashMap::new();
        let mut q = VecDeque::new();
        q.push_back(initial.to_vec());
        while let Some(m) = q.pop_front() {
            for ti in 0..self.transition_names.len() {
                if let Some(n) = self.ref_fire(&m, ti) {
                    if !parent.contains_key(&n) && n != *initial {
                        parent.insert(n.clone(), (m.clone(), ti));
                        if n == target {
                            let mut seq = Vec::new();
                            let mut cur = n;
                            while let Some((p, t)) = parent.remove(&cur) {
                                seq.push(self.transition_names[t].clone());
                                cur = p;
                            }
                            seq.reverse();
                            return Some(seq);
                        }
                        q.push_back(n);
                    }
                }
            }
        }
        None
    }

    /// 某标识下使能的变迁集合（按名字）。
    pub fn enabled_names(&self, m: &[i64]) -> Vec<String> {
        (0..self.transition_names.len())
            .filter(|&ti| self.ref_fire(m, ti).is_some())
            .map(|ti| self.transition_names[ti].clone())
            .collect()
    }
}
