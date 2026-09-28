//! 路由装配：把处理器、状态与请求标识中间件组合成 Axum Router。

use axum::extract::Extension;
use axum::middleware;
use axum::routing::{delete, get, post};
use axum::Router;

use super::handlers::{
    apply, build, create_manager, delete_manager, equivalence, evaluate, gc, health, list_roots,
    restrict, VarCap,
};
use super::request_id;
use super::state::AppState;

/// 构建应用路由。
pub fn build_router(state: AppState, var_cap: usize) -> Router {
    Router::new()
        .route("/healthz", get(health))
        .route("/v1/managers", post(create_manager))
        .route("/v1/managers/{manager_id}", delete(delete_manager))
        .route("/v1/managers/{manager_id}/build", post(build))
        .route("/v1/managers/{manager_id}/apply", post(apply))
        .route("/v1/managers/{manager_id}/restrict", post(restrict))
        .route("/v1/managers/{manager_id}/evaluate", post(evaluate))
        .route("/v1/managers/{manager_id}/gc", post(gc))
        .route("/v1/managers/{manager_id}/roots", get(list_roots))
        .route("/v1/equivalence", post(equivalence))
        // 变量穷举上限通过扩展层注入，处理器以 Extension<VarCap> 取出。
        .layer(Extension(VarCap(var_cap)))
        .layer(middleware::from_fn(request_id::layer))
        .with_state(state)
}
