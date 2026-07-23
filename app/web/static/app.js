/* ==========================================================================
   AI 行业情报 · 生产交互脚本
   生产表单、筛选与操作交互
   ========================================================================== */
"use strict";

(function () {
  var STATE_KEY = "ai-intel-state";
  var state = loadState();

  function loadState() {
    try {
      var raw = localStorage.getItem(STATE_KEY);
      return raw ? JSON.parse(raw) : { view: "a", openEditors: {}, openMores: {}, openRowExpands: {} };
    } catch (_) {
      return { view: "a", openEditors: {}, openMores: {}, openRowExpands: {} };
    }
  }

  function saveState() {
    try {
      localStorage.setItem(STATE_KEY, JSON.stringify(state));
    } catch (_) { /* ignore */ }
  }

  function $ (sel, root) {
    return (root || document).querySelector(sel);
  }
  function $all(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  /* ---------- Toast ---------- */
  var toastTimer = null;
  function toast(message) {
    var el = $("#toast");
    if (!el) { el = document.createElement("div"); el.className = "toast"; el.id = "toast"; el.setAttribute("role", "status"); el.setAttribute("aria-live", "polite"); document.body.appendChild(el); }
    el.textContent = message;
    el.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 2600);
  }

  function submitPost(path, values) {
    var form = document.createElement("form");
    form.method = "post";
    form.action = path;
    form.hidden = true;
    Object.keys(values).forEach(function (name) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = values[name];
      form.appendChild(input);
    });
    document.body.appendChild(form);
    form.submit();
  }

  function postAction(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body
    }).then(function (response) {
      if (!response.ok) throw new Error("操作未完成");
      return response;
    });
  }

  /* ---------- 更新按钮 loading ---------- */
  function bindUpdateButtons() {
    $all("[data-update-form]").forEach(function (form) {
      if (!(form instanceof HTMLFormElement)) return;
      form.addEventListener("submit", function () {
        var btn = form.querySelector("button[type='submit']");
        if (btn) {
          btn.disabled = true;
          btn.setAttribute("aria-busy", "true");
          var original = btn.textContent;
          btn.textContent = btn.getAttribute("data-processing-text") || "处理中…";
          setTimeout(function () {
            if (btn.disabled) { btn.textContent = original; btn.disabled = false; }
          }, 10000);
        }
      });
    });
  }

  /* ---------- 列表样式切换 ---------- */
  function bindViewSwitch() {
    var listA = $("#list-a");
    var listB = $("#list-b");
    if (!listA || !listB) return;

    $all(".view-switch button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var view = btn.getAttribute("data-view");
        if (!view) return;
        state.view = view;
        saveState();
        $all(".view-switch button").forEach(function (b) {
          b.classList.toggle("is-active", b === btn);
        });
        listA.hidden = view !== "a";
        listB.hidden = view !== "b";
        updateBatchUI();
      });
    });

    if (state.view === "b") {
      var btnB = $('.view-switch button[data-view="b"]');
      var btnA = $('.view-switch button[data-view="a"]');
      if (btnB && btnA) { btnB.classList.add("is-active"); btnA.classList.remove("is-active"); listA.hidden = true; listB.hidden = false; }
    }
  }

  /* ---------- 筛选：更多筛选折叠 ---------- */
  function bindMoreFilters() {
    var btn = $("#btn-more-filters");
    var panel = $("#more-filters");
    if (!btn || !panel) return;
    btn.addEventListener("click", function () {
      var open = panel.hidden;
      panel.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      btn.firstChild.textContent = open ? "收起筛选 " : "更多筛选 ";
    });
    $("#btn-clear-filter") && $("#btn-clear-filter").addEventListener("click", function () {
      $("#filter-form") && $("#filter-form").reset();
    });
  }

  /* ---------- 批量选择 ---------- */
  function itemCheckboxes(checkedOnly) {
    var root = state.view === "b" ? $("#list-b") : $("#list-a");
    if (!root) return [];
    return $all(checkedOnly ? ".item-checkbox:checked" : ".item-checkbox", root);
  }

  function updateBatchUI() {
    var checked = itemCheckboxes(true);
    var ids = checked.map(function (cb) { return cb.value || cb.getAttribute("data-select"); }).filter(Boolean);
    var count = ids.length;
    var batchEl = $("#batch-actions");
    if (batchEl) {
      $all(".btn", batchEl).forEach(function (b) { b.disabled = count === 0; });
    }
    var countEl = $("#selected-count");
    if (countEl) {
      countEl.hidden = count === 0;
      countEl.textContent = "已选 " + count + " 条";
    }
    // 更新隐藏域
    var batchIds = $("#batch-item-ids");
    if (batchIds) batchIds.value = ids.join(",");
    var batchAiIds = $("#batch-ai-item-ids");
    if (batchAiIds) batchAiIds.value = ids.join(",");
    // 全选框
    var selectAll = $("#select-all");
    if (selectAll) {
      var allItems = itemCheckboxes(false);
      selectAll.checked = count > 0 && count === allItems.length;
      selectAll.indeterminate = count > 0 && count < allItems.length;
    }
  }

  function bindSelection() {
    var selectAll = $("#select-all");
    if (selectAll) {
      selectAll.addEventListener("change", function (e) {
        var on = e.target.checked;
        itemCheckboxes(false).forEach(function (cb) {
          cb.checked = on;
        });
        updateBatchUI();
      });
    }
    document.body.addEventListener("change", function (e) {
      if (e.target.matches(".item-checkbox, [data-select]")) {
        updateBatchUI();
      }
    });
  }

  /* ---------- 批量操作 ---------- */
  function bindBatchActions() {
    $all("#batch-actions .btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var action = btn.getAttribute("data-batch");
        if (!action) return;
        var ids = [];
        itemCheckboxes(true).forEach(function (cb) {
          ids.push(cb.value || cb.getAttribute("data-select"));
        });
        if (ids.length === 0) { toast("请先勾选资讯"); return; }

        if (action === "read" || action === "unread") {
          submitPost("/items/batch-read", { item_ids: ids.join(","), is_read: action === "read" ? "true" : "false", return_to: window.location.pathname + window.location.search });
        } else if (action === "ai-classify") {
          submitPost("/items/batch-ai-classify", { item_ids: ids.join(","), return_to: window.location.pathname + window.location.search });
        } else if (action === "ai-summarize") {
          submitPost("/items/batch-ai-summarize", { item_ids: ids.join(","), return_to: window.location.pathname + window.location.search });
        }
      });
    });
  }

  /* ---------- 单条操作：收藏、已读 via AJAX ---------- */
  document.body.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-act]");
    if (!btn) return;

    var act = btn.getAttribute("data-act");
    var article = btn.closest("[data-id]");
    if (!article) return;
    var id = article.getAttribute("data-id");

    if (act === "open") {
      postAction("/items/" + id + "/read", "is_read=true&return_to=" + encodeURIComponent(window.location.pathname + window.location.search)).then(function () {
        article.classList.remove("is-unread");
      }).catch(function () { toast("原文已打开，但未能标为已读"); });
      return;
    }

    if (act === "fav") {
      var favInput = article.querySelector('input[name="favorite"]');
      var isFav = favInput ? favInput.value === "true" : false;
      postAction("/items/" + id + "/favorite", "favorite=" + (isFav ? "false" : "true") + "&return_to=" + encodeURIComponent(window.location.pathname + window.location.search)).then(function () {
        location.reload();
      }).catch(function () { toast("收藏操作未完成"); });
      return;
    }

    if (act === "toggle-read") {
      var readInput = article.querySelector('input[name="is_read"]');
      var isRead = readInput ? readInput.value === "true" : false;
      postAction("/items/" + id + "/read", "is_read=" + (isRead ? "false" : "true") + "&return_to=" + encodeURIComponent(window.location.pathname + window.location.search)).then(function () {
        location.reload();
      }).catch(function () { toast("阅读状态未能保存"); });
      return;
    }

    if (act === "ai-classify") {
      postAction("/items/" + id + "/ai-classify", "return_to=" + encodeURIComponent(window.location.pathname + window.location.search)).then(function () {
        toast("AI 分类任务已提交");
      }).catch(function () { toast("AI 分类任务提交失败"); });
      return;
    }

    if (act === "ai-summarize") {
      postAction("/items/" + id + "/ai-summarize", "return_to=" + encodeURIComponent(window.location.pathname + window.location.search)).then(function () {
        toast("AI 总结任务已提交");
      }).catch(function () { toast("AI 总结任务提交失败"); });
      return;
    }

    if (act === "toggle-summary") {
      var summary = article.querySelector(".item-summary");
      if (summary) {
        var expanded = summary.classList.toggle("is-expanded");
        btn.textContent = expanded ? "收起" : "展开全部";
      }
      return;
    }

    if (act === "edit-category") {
      var editor = article.querySelector("[data-editor]");
      var more = article.querySelector("[data-more]");
      if (editor) editor.hidden = !editor.hidden;
      if (more && !more.hidden) more.hidden = true;
      return;
    }

    if (act === "toggle-more") {
      var morePanel = article.querySelector("[data-more]");
      var edtr = article.querySelector("[data-editor]");
      if (morePanel) morePanel.hidden = !morePanel.hidden;
      if (edtr && !edtr.hidden) edtr.hidden = true;
      return;
    }

    if (act === "toggle-row-more") {
      var rowExpand = article.querySelector("[data-row-expand]");
      var rowBtn = article.querySelector('.row-action[data-act="toggle-row-more"]');
      if (rowExpand) {
        var isOpen = !rowExpand.hidden;
        rowExpand.hidden = !isOpen;
        if (rowBtn) rowBtn.textContent = isOpen ? "▾" : "▴";
      }
      return;
    }

    if (act === "save-category") {
      var select = article.querySelector("[data-category-select]");
      var editor = article.querySelector("[data-editor]");
      if (!select) return;
      var val = select.value;
      postAction("/items/" + id + "/category", "category=" + encodeURIComponent(val) + "&return_to=" + encodeURIComponent(window.location.pathname + window.location.search)).then(function () {
        if (editor) editor.hidden = true;
        var chip = article.querySelector(".category-chip");
        if (chip && val) {
          var labels = {};
          var options = select.querySelectorAll("option");
          options.forEach(function (o) { if (o.value) labels[o.value] = o.textContent; });
          chip.textContent = labels[val] || val;
          chip.classList.toggle("is-pending", labels[val] === "待分类");
        }
        if (!val) {
          toast("已清除人工分类");
        } else {
          toast("已将分类修改为「" + (select.querySelector("option[value='" + val + "']") || {}).textContent || val + "」");
        }
      }).catch(function () { toast("分类修改未能保存"); });
      return;
    }

    if (act === "cancel-category") {
      var edt = article.querySelector("[data-editor]");
      var rowExp = article.querySelector("[data-row-expand]");
      if (edt) edt.hidden = true;
      if (rowExp) {
        rowExp.hidden = true;
        var rb = article.querySelector('.row-action[data-act="toggle-row-more"]');
        if (rb) rb.textContent = "▾";
      }
      return;
    }
  });

  /* ---------- AI 页面模式联动 ---------- */
  function bindAIModes() {
    function bindMode(listId, extraId) {
      var list = document.getElementById(listId);
      if (!list) return;
      list.addEventListener("change", function (e) {
        var radio = e.target.closest('input[type="radio"]');
        if (!radio) return;
        $all(".option-row", list).forEach(function (row) {
          row.classList.toggle("is-selected", row.querySelector("input").checked);
        });
        if (extraId) {
          var extra = document.getElementById(extraId);
          if (extra) extra.hidden = radio.value !== "auto";
        }
      });
    }
    bindMode("classifier-mode", "classifier-strategy");
    bindMode("summarizer-mode", null);

    var testBtn = document.getElementById("ai-test");
    if (testBtn) {
      testBtn.addEventListener("click", function () {
        testBtn.disabled = true;
        testBtn.textContent = "正在测试…";
      });
    }
  }

  /* ---------- 设置页开关联动 ---------- */
  function bindSettingsSwitch() {
    var enabled = document.getElementById("setting-enabled");
    if (!enabled) return;
    enabled.addEventListener("change", function (e) {
      var on = e.target.checked;
      var fields = document.getElementById("schedule-fields");
      if (fields) {
        $all("input, select", fields).forEach(function (el) { el.disabled = !on; });
      }
      var status = document.getElementById("scheduler-status");
      if (status) {
        status.textContent = on ? "等待下一次运行" : "已关闭";
        status.className = "status " + (on ? "status-ok" : "status-muted");
      }
    });
  }

  /* ---------- 表格展开行（AI 任务 / 来源错误 / 运行记录） ---------- */
  function bindExpandableRows() {
    document.body.addEventListener("click", function (e) {
      var tr = e.target.closest("[data-job], [data-source], [data-run]");
      if (!tr) return;
      if (e.target.closest(".row-ops") || e.target.closest("button") || e.target.closest("a") || e.target.closest("input")) return;

      var id;
      if (tr.hasAttribute("data-job")) { id = "job-" + tr.getAttribute("data-job"); }
      else if (tr.hasAttribute("data-source")) { id = "src-" + tr.getAttribute("data-source"); tr = tr.closest("tbody") ? tr.parentElement.querySelector('.tr-expand[id]') || tr.nextElementSibling : null; }
      else if (tr.hasAttribute("data-run")) { id = "run-" + tr.getAttribute("data-run"); }

      if (!tr) return;
      var expandRow;
      if (tr.nextElementSibling && tr.nextElementSibling.classList.contains("tr-expand")) {
        expandRow = tr.nextElementSibling;
      }

      if (expandRow) {
        var open = expandRow.classList.toggle("is-open");
        tr.classList.toggle("is-open", open);
      }
    });
  }

  /* ---------- 来源页来源错误展开 ---------- */
  function bindSourceErrToggle() {
    document.body.addEventListener("click", function (e) {
      var btn = e.target.closest('[data-act="toggle-source-err"]');
      if (!btn) return;
      var tr = btn.closest("tr");
      if (!tr) return;
      var expandRow;
      if (tr.nextElementSibling && tr.nextElementSibling.classList.contains("tr-expand")) {
        expandRow = tr.nextElementSibling;
      }
      if (expandRow) {
        var open = !expandRow.classList.contains("is-open");
        expandRow.classList.toggle("is-open", open);
        btn.textContent = open ? "收起错误" : "查看错误";
      }
    });
  }

  /* ---------- 来源候选启用确认 ---------- */
  function bindCandidateActivate() {
    document.body.addEventListener("click", function (e) {
      var btn = e.target.closest('[data-act="activate-candidate"]');
      if (!btn) return;
      if (!window.confirm("启用该来源并加入监控？启用后将参与每次批量更新，抓取资讯进入首页与导出。")) {
        e.preventDefault();
      }
    });
  }

  /* ---------- 初始化和 JSON 回显 ---------- */
  function checkSavedMessages() {
    if (document.querySelector(".notice.is-success, .alert, .notice.success")) {
      // 后端回显的消息直接显示，不额外弹 toast
    }
    if (window.location.search.indexOf("updated=1") > -1) {
      toast("来源设置已更新");
    }
    if (window.location.search.indexOf("saved=1") > -1) {
      toast("设置已保存");
    }
  }

  /* ---------- 清除 Key 二次确认 ---------- */
  function bindClearKey() {
    var btn = document.getElementById("ai-clear-key");
    if (!btn) return;
    btn.addEventListener("click", function (e) {
      if (!window.confirm("确认清除已保存的 API Key？清除后 AI 分类与总结将不可用。")) {
        e.preventDefault();
      }
    });
  }

  /* ---------- 启动 ---------- */
  document.addEventListener("DOMContentLoaded", function () {
    bindUpdateButtons();
    bindViewSwitch();
    bindMoreFilters();
    bindSelection();
    bindBatchActions();
    bindAIModes();
    bindSettingsSwitch();
    bindExpandableRows();
    bindSourceErrToggle();
    bindCandidateActivate();
    bindClearKey();
    updateBatchUI();
    checkSavedMessages();
  });

  /* ---------- 暴露给外部 ---------- */
  window.markRead = function (itemId) {
    postAction("/items/" + itemId + "/read", "is_read=true&return_to=" + encodeURIComponent(window.location.pathname + window.location.search)).then(function () {
      var card = document.querySelector('[data-id="' + itemId + '"]');
      if (card) card.classList.remove("is-unread");
    });
  };
  window.updateBatchSelection = updateBatchUI;
  window.batchRead = function (isRead) {
    var checked = itemCheckboxes(true);
    if (checked.length === 0) { toast("请先勾选资讯"); return; }
    var ids = checked.map(function (c) { return c.value || c.getAttribute("data-select"); });
    submitPost("/items/batch-read", { item_ids: ids.join(","), is_read: isRead ? "true" : "false", return_to: window.location.pathname + window.location.search });
  };
  window.batchAIClassify = function () {
    var checked = itemCheckboxes(true);
    if (checked.length === 0) { toast("请先勾选资讯"); return; }
    if (!window.confirm("对勾选的 " + checked.length + " 条资讯执行 AI 分类？")) return;
    var ids = checked.map(function (c) { return c.value || c.getAttribute("data-select"); });
    submitPost("/items/batch-ai-classify", { item_ids: ids.join(","), return_to: window.location.pathname + window.location.search });
  };

})();
