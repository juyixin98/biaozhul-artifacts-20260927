use fsm_core::Budget;
use fsm_lang::compile::CompiledSpec;
use fsm_lang::Spec;

pub fn load_fixture(name: &str) -> CompiledSpec {
    let path = format!("{}/../../fixtures/{name}.json", env!("CARGO_MANIFEST_DIR"));
    let text = std::fs::read_to_string(&path).unwrap();
    let model: Spec = serde_json::from_str(&text).unwrap();
    CompiledSpec::compile(&model).unwrap()
}

pub fn spec_budget(_spec: &CompiledSpec) -> Budget {
    Budget {
        max_states: 100_000,
        max_transitions: 1_000_000,
        max_initial_scan: 1_000_000,
    }
}
