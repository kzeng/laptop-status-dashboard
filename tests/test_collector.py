import json

import collector


def test_collect_schedule_combines_user_sources(monkeypatch):
    outputs = {
        ("crontab", "-l"): "30 5 * * 0 bash /srv/deploy-site.sh\n30 20 * * * bash /srv/download_bing_wallpaper.sh",
        ("systemctl", "--user", "list-timers", "--all", "--no-pager", "--output=json"): json.dumps(
            [{"next": 1_800_000_000_000_000, "unit": "route-monitor.timer", "activates": "route-monitor.service"}]
        ),
        ("openclaw", "cron", "list", "--json"): json.dumps(
            {
                "jobs": [
                    {
                        "name": "Memory maintenance",
                        "enabled": True,
                        "schedule": {"kind": "cron", "expr": "0 3 * * *"},
                        "nextRunAtMs": 1_800_000_000_000,
                    }
                ]
            }
        ),
        ("systemctl", "list-timers", "--all", "--no-pager", "--output=json"): json.dumps(
            [{"unit": "apt-daily.timer"}, {"unit": "fstrim.timer"}]
        ),
    }

    monkeypatch.setattr(collector, "_run", lambda command, timeout=3: outputs.get(tuple(command), ""))

    result = collector.collect_schedule()

    assert result["user_count"] == 4
    assert result["system_timer_count"] == 2
    assert [item["source"] for item in result["items"]] == [
        "用户 Cron",
        "用户 Cron",
        "用户 Systemd",
        "OpenClaw",
    ]
    assert result["items"][0]["name"] == "mitoto 新闻站部署"
    assert result["items"][1]["name"] == "下载 Bing 壁纸"
    assert all("command" not in item for item in result["items"])


def test_collect_schedule_tolerates_missing_sources(monkeypatch):
    monkeypatch.setattr(collector, "_run", lambda command, timeout=3: "")

    assert collector.collect_schedule() == {"items": [], "user_count": 0, "system_timer_count": 0}
