// Cole esse código no Google Apps Script da sua planilha
// Passos: Extensões → Apps Script → cole aqui → Implantar → Web app → Qualquer pessoa

var SALDO_INICIAL = 35385.11; // Saldo inicial em 11/06/2026

function doPost(e) {
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    var data  = JSON.parse(e.postData.contents);

    if (sheet.getLastRow() === 0) {
      sheet.appendRow(["Data", "Hora", "Descrição", "Valor (R$)", "Tipo", "Categoria"]);
      sheet.getRange(1, 1, 1, 6).setFontWeight("bold");
    }

    sheet.appendRow([
      data.data,
      data.hora,
      data.descricao,
      data.valor,
      data.tipo,
      data.categoria
    ]);

    return ContentService
      .createTextOutput(JSON.stringify({ status: "ok" }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ status: "erro", msg: err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet(e) {
  try {
    var sheet    = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    var lastRow  = sheet.getLastRow();
    var now      = new Date();
    var mesAtual = now.getMonth() + 1;
    var anoAtual = now.getFullYear();

    var totalEntradas = 0;
    var totalSaidas   = 0;
    var entradasMes   = 0;
    var saidasMes     = 0;
    var topCats       = {};

    if (lastRow >= 2) {
      var dados = sheet.getRange(2, 1, lastRow - 1, 6).getValues();

      dados.forEach(function(row) {
        var valor     = parseFloat(row[3]) || 0;
        var tipo      = row[4];
        var dataStr   = String(row[0]);
        var categoria = row[5] || "Outros";

        if (tipo === "Entrada") {
          totalEntradas += valor;
        } else {
          totalSaidas += valor;
          topCats[categoria] = (topCats[categoria] || 0) + valor;
        }

        var parts = dataStr.split("/");
        if (parts.length === 3) {
          var mes = parseInt(parts[1]);
          var ano = parseInt(parts[2]);
          if (mes === mesAtual && ano === anoAtual) {
            if (tipo === "Entrada") entradasMes += valor;
            else saidasMes += valor;
          }
        }
      });
    }

    var saldoAtual = SALDO_INICIAL + totalEntradas - totalSaidas;

    var sortedCats = Object.entries(topCats)
      .sort(function(a, b) { return b[1] - a[1]; })
      .slice(0, 3);

    return ContentService
      .createTextOutput(JSON.stringify({
        saldo:          saldoAtual,
        entradas_mes:   entradasMes,
        saidas_mes:     saidasMes,
        resultado_mes:  entradasMes - saidasMes,
        top_categorias: sortedCats
      }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ status: "erro", msg: err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
