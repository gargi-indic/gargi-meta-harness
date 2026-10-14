# Fix missing tests by appending them
with open("tests/github/test_outbox.py", "a") as f:
    f.write("""

@pytest.mark.asyncio
async def test_three_coalesced_upsert_ledgers(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    outbox = Outbox(store, gh, clock=clock)
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "first"}, coalesce_key="ledger_1")
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "second"}, coalesce_key="ledger_1")
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "third"}, coalesce_key="ledger_1")

    respx_mock.get("https://api.github.com/repos/a/b/issues/1/comments").respond(
        200, json=[{"id": 11, "body": f"{LEDGER_MARKER}\\nold ledger\\n<!-- harness:state v1 z:eJwB= -->"}]
    )

    def match_patch(request):
        body = json.loads(request.content)
        assert body["body"] == "third"
        return httpx.Response(200, json={"id": 11})

    patch_mock = respx_mock.patch("https://api.github.com/repos/a/b/issues/comments/11").mock(side_effect=match_patch)

    sent = await outbox.drain_once()
    assert sent == 1
    assert patch_mock.call_count == 1


@pytest.mark.asyncio
async def test_upsert_ledger_create_then_edit(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    called_ids = []
    async def callback(repo, issue, comment_id):
        called_ids.append(comment_id)

    outbox = Outbox(store, gh, clock=clock, on_ledger_comment=callback)

    # First drain: creates the comment
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "new ledger"})
    respx_mock.get("https://api.github.com/repos/a/b/issues/1/comments").respond(200, json=[])
    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(201, json={"id": 100})

    sent = await outbox.drain_once()
    assert sent == 1
    assert called_ids == [100]

    # Second drain: updates the comment
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "updated ledger"})
    respx_mock.get("https://api.github.com/repos/a/b/issues/1/comments").respond(
        200, json=[{"id": 100, "body": f"{LEDGER_MARKER}\\nold ledger\\n<!-- harness:state v1 z:eJwB= -->"}]
    )
    respx_mock.patch("https://api.github.com/repos/a/b/issues/comments/100").respond(200, json={"id": 100})

    sent = await outbox.drain_once()
    assert sent == 1
    assert called_ids == [100, 100]


@pytest.mark.asyncio
async def test_restart_file_db(respx_mock, tmp_path, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    db_path = tmp_path / "test.db"
    store1 = Store(db_path)
    store1.migrate()

    outbox1 = Outbox(store1, gh, clock=clock)
    outbox1.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "persisted"})
    store1.close()

    store2 = Store(db_path)
    store2.migrate()
    outbox2 = Outbox(store2, gh, clock=clock)
    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(201, json={"id": 1})

    sent = await outbox2.drain_once()
    assert sent == 1
    store2.close()


@pytest.mark.asyncio
async def test_drain_order_by_id(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    outbox = Outbox(store, gh, clock=clock)

    outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "first"})
    outbox.enqueue("add_comment", {"repo": "a/b", "issue": 2, "body": "second"})

    order = []
    def match_post1(request):
        order.append(1)
        return httpx.Response(201, json={"id": 1})

    def match_post2(request):
        order.append(2)
        return httpx.Response(201, json={"id": 2})

    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").mock(side_effect=match_post1)
    respx_mock.post("https://api.github.com/repos/a/b/issues/2/comments").mock(side_effect=match_post2)

    sent = await outbox.drain_once()
    assert sent == 2
    assert order == [1, 2]


@pytest.mark.asyncio
async def test_set_labels_add_and_remove(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    outbox = Outbox(store, gh, clock=clock)
    outbox.enqueue("set_labels", {"repo": "a/b", "issue": 1, "add": ["label1"], "remove": ["label2"]})

    # 7 labels ensured
    from harness.github.labels import HARNESS_LABELS
    for name in HARNESS_LABELS:
        respx_mock.post("https://api.github.com/repos/a/b/labels").respond(201, json={"name": name})

    respx_mock.post("https://api.github.com/repos/a/b/issues/1/labels").respond(201, json=[])
    respx_mock.delete("https://api.github.com/repos/a/b/issues/1/labels/label2").respond(200)

    sent = await outbox.drain_once()
    assert sent == 1


@pytest.mark.asyncio
async def test_close_issue(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    outbox = Outbox(store, gh, clock=clock)
    outbox.enqueue("close_issue", {"repo": "a/b", "issue": 1})

    def match_patch(request):
        body = json.loads(request.content)
        assert body["state"] == "closed"
        return httpx.Response(200, json={})

    respx_mock.patch("https://api.github.com/repos/a/b/issues/1").mock(side_effect=match_patch)
    sent = await outbox.drain_once()
    assert sent == 1


@pytest.mark.asyncio
async def test_run_with_fake_sleep(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    sleep_count = 0
    stop_event = asyncio.Event()

    async def fake_sleep(duration):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 2:
            stop_event.set()

    outbox = Outbox(store, gh, clock=clock, sleep=fake_sleep)
    await outbox.run(stop_event, idle_sleep=2.0)
    assert sleep_count == 2

""")
