package store_test

import (
	"context"
	"net/netip"
	"testing"
	"time"

	"ipfragreasm/internal/netmodel"
	"ipfragreasm/internal/store"
)

func mustParseAddr(s string) netip.Addr {
	a, err := netip.ParseAddr(s)
	if err != nil {
		panic(err)
	}
	return a
}

func testKey(id uint16) netmodel.FragKey {
	return netmodel.FragKey{
		Src:      addr("10.0.0.1"),
		Dst:      addr("10.0.0.2"),
		Protocol: netmodel.ProtoUDP,
		ID:       id,
	}
}

func addr(s string) netip.Addr { return mustParseAddr(s) }

// conformanceSuite 对任意 Store 实现跑同一组行为断言。
func conformanceSuite(t *testing.T, st store.Store) {
	t.Helper()
	ctx := context.Background()
	now := time.Unix(1_700_000_000, 0)
	key := testKey(0x1111)

	// 初始为空。
	if _, found, err := st.GetGroup(ctx, key); err != nil || found {
		t.Fatalf("初始 GetGroup 应为空: found=%v err=%v", found, err)
	}
	opens, err := st.ListOpenGroups(ctx)
	if err != nil || len(opens) != 0 {
		t.Fatalf("初始 ListOpenGroups 应为空: %d %v", len(opens), err)
	}

	// 写入 pending 组与两片。
	if err := st.UpsertGroup(ctx, store.GroupRecord{
		Key: key, State: store.StatePending,
		StartedAt: now, Deadline: now.Add(time.Second),
	}); err != nil {
		t.Fatalf("UpsertGroup: %v", err)
	}
	for i, off := range []int{0, 8} {
		if err := st.AddFragment(ctx, store.FragmentRecord{
			Key: key, Seq: i, Offset: off, Length: 8,
			More: true, SeenAt: now, Payload: make([]byte, 8),
		}); err != nil {
			t.Fatalf("AddFragment %d: %v", i, err)
		}
	}
	frags, err := st.ListFragments(ctx, key)
	if err != nil || len(frags) != 2 || frags[0].Seq != 0 || frags[1].Offset != 8 {
		t.Fatalf("ListFragments 顺序/数量错误: %+v err=%v", frags, err)
	}
	if opens, _ := st.ListOpenGroups(ctx); len(opens) != 1 {
		t.Fatalf("应有 1 个 open 组")
	}

	// 终结：删分片、组行保留，过期时间未到不应回收。
	if err := st.DeleteFragments(ctx, key); err != nil {
		t.Fatalf("DeleteFragments: %v", err)
	}
	if frags, _ := st.ListFragments(ctx, key); len(frags) != 0 {
		t.Fatalf("终结后分片应为 0")
	}
	expiry := now.Add(time.Minute)
	if err := st.UpsertGroup(ctx, store.GroupRecord{
		Key: key, State: store.StateComplete,
		StartedAt: now, Deadline: now.Add(time.Second),
		TerminalAt: now, ExpiresAt: expiry,
		Assembled: []byte{1, 2, 3, 4},
	}); err != nil {
		t.Fatalf("终结 Upsert: %v", err)
	}
	if opens, _ := st.ListOpenGroups(ctx); len(opens) != 0 {
		t.Fatalf("终结组不应出现在 open 列表")
	}
	if expired, _ := st.ListTerminalExpired(ctx, now.Add(30*time.Second)); len(expired) != 0 {
		t.Fatalf("未到 TTL 不应回收")
	}
	rec, found, _ := st.GetGroup(ctx, key)
	if !found || rec.State != store.StateComplete || string(rec.Assembled) != "\x01\x02\x03\x04" {
		t.Fatalf("终结组审计内容错误: %+v found=%v", rec, found)
	}

	// 到 TTL 后应可列出，再彻底删除。
	expired, err := st.ListTerminalExpired(ctx, expiry)
	if err != nil || len(expired) != 1 {
		t.Fatalf("到 TTL 应列出 1 个: %d %v", len(expired), err)
	}
	if err := st.DeleteGroup(ctx, key); err != nil {
		t.Fatalf("DeleteGroup: %v", err)
	}
	if _, found, _ := st.GetGroup(ctx, key); found {
		t.Fatalf("删除后不应再找到")
	}
	if all, _ := st.ListAllGroups(ctx); len(all) != 0 {
		t.Fatalf("删除后应无组")
	}
}

func TestMemoryStoreConformance(t *testing.T) {
	conformanceSuite(t, store.NewMemory())
}

func TestSQLiteStoreConformance(t *testing.T) {
	ctx := context.Background()
	dsn := "file:" + t.TempDir() + "/test.sqlite"
	st, err := store.OpenSQLite(ctx, dsn)
	if err != nil {
		t.Fatalf("打开 SQLite: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	conformanceSuite(t, st)
}

// TestSQLiteRecoveryAcrossReopen 验证崩溃/重启恢复语义：
// 重开同一 DSN，pending 组与其分片必须能被重新列出。
func TestSQLiteRecoveryAcrossReopen(t *testing.T) {
	ctx := context.Background()
	path := t.TempDir() + "/recover.sqlite"
	st, err := store.OpenSQLite(ctx, "file:"+path)
	if err != nil {
		t.Fatalf("open1: %v", err)
	}
	key := testKey(0x2222)
	now := time.Unix(1_700_000_000, 0)
	if err := st.UpsertGroup(ctx, store.GroupRecord{
		Key: key, State: store.StatePending,
		StartedAt: now, Deadline: now.Add(time.Second),
	}); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	if err := st.AddFragment(ctx, store.FragmentRecord{
		Key: key, Seq: 0, Offset: 0, Length: 8, More: true,
		SeenAt: now, Payload: []byte{9, 9, 9, 9, 9, 9, 9, 9},
	}); err != nil {
		t.Fatalf("addfrag: %v", err)
	}
	if err := st.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	st2, err := store.OpenSQLite(ctx, "file:"+path)
	if err != nil {
		t.Fatalf("open2: %v", err)
	}
	defer st2.Close()
	opens, err := st2.ListOpenGroups(ctx)
	if err != nil || len(opens) != 1 || opens[0].Key != key {
		t.Fatalf("重开后应恢复 1 个 pending 组: %+v err=%v", opens, err)
	}
	frags, err := st2.ListFragments(ctx, key)
	if err != nil || len(frags) != 1 || frags[0].Payload[0] != 9 {
		t.Fatalf("重开后分片恢复错误: %+v err=%v", frags, err)
	}
}
