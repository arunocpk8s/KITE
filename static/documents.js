// The document editor and comments on selected text. Without JavaScript the editor stays a plain text box.
// The server cleans whatever is saved, so nothing here is relied on for safety.
(function () {
  "use strict";

  function link(url, text, embed) {
    var a = document.createElement("a");
    a.href = url;
    a.textContent = text;
    if (embed) a.className = "doc-embed";
    return a.outerHTML;
  }

  function askUrl() {
    var url = (window.prompt("Link (https://...)") || "").trim();
    if (!url) return null;
    if (!/^https:\/\//i.test(url)) {
      window.alert("The link must start with https://");
      return null;
    }
    return url;
  }

  function setUpEditor(form) {
    var source = form.querySelector(".doc-source");
    var editor = form.querySelector(".doc-editor");
    var toolbar = form.querySelector(".doc-toolbar");
    // The source was cleaned and escaped by KITE before it reached this page.
    editor.innerHTML = source.value;
    editor.hidden = toolbar.hidden = false;
    source.hidden = true;

    var saved = source.value;
    var range = null;
    document.addEventListener("selectionchange", function () {
      var selection = window.getSelection();
      if (selection.rangeCount && editor.contains(selection.anchorNode)) range = selection.getRangeAt(0);
    });
    function focus() {
      editor.focus();
      if (range) {
        var selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
      }
    }

    toolbar.addEventListener("mousedown", function (event) {
      if (event.target.closest("button")) event.preventDefault(); // keeps the selection in the document
    });
    toolbar.addEventListener("click", function (event) {
      var button = event.target.closest("button");
      if (!button) return;
      var insert = button.dataset.insert;
      if (button.dataset.cmd) {
        focus();
        document.execCommand(button.dataset.cmd);
      } else if (button.dataset.block) {
        focus();
        document.execCommand("formatBlock", false, button.dataset.block);
      } else if (insert === "table") {
        focus();
        document.execCommand("insertHTML", false, "<table><tbody><tr><th><br></th><th><br></th><th><br></th></tr>" +
          "<tr><td><br></td><td><br></td><td><br></td></tr></tbody></table><p><br></p>");
      } else if (insert === "link" || insert === "embed") {
        var url = askUrl();
        if (!url) return;
        focus();
        var selected = window.getSelection().toString();
        document.execCommand("insertHTML", false, link(url, selected || url, insert === "embed") + "&nbsp;");
      }
    });

    var submitting = false;
    form.addEventListener("submit", function () {
      submitting = true;
      source.value = editor.innerHTML;
    });
    window.addEventListener("beforeunload", function (event) {
      if (!submitting && editor.innerHTML !== saved) event.preventDefault();
    });
    saved = editor.innerHTML;
    document.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        form.requestSubmit();
      }
    });
  }

  function setUpComments(read, form) {
    var quote = form.querySelector("[name=quote]");
    var preview = form.querySelector(".doc-quote-preview");
    document.addEventListener("selectionchange", function () {
      var selection = window.getSelection();
      if (selection.isCollapsed || !read.contains(selection.anchorNode)) return;
      quote.value = preview.textContent = selection.toString().trim().slice(0, 500);
      preview.hidden = !quote.value;
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.querySelector("[data-doc-form]");
    if (form) setUpEditor(form);
    var read = document.querySelector("[data-doc-read]");
    var commentForm = document.querySelector("[data-comment-form]");
    if (read && commentForm) setUpComments(read, commentForm);
  });
})();
