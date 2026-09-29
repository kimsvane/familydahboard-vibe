'use strict';

// Hele skærmen er trygbar. pointerdown fanger både mus, touch og pen,
// så et barn ikke skal ramme en bestemt knap.
const flade = document.getElementById('vack');
const tid = document.getElementById('tid');

function opdaterTid() {
  const nu = new Date();
  tid.textContent = nu.toLocaleTimeString('da-DK', { hour: '2-digit', minute: '2-digit' });
}

function vaagn() {
  window.familyDashboard?.wake();
}

flade.addEventListener('pointerdown', vaagn);
flade.addEventListener('keydown', vaagn);
document.addEventListener('click', vaagn);
document.addEventListener('contextmenu', (event) => event.preventDefault());
window.addEventListener('focus', () => document.body.focus());

opdaterTid();
setInterval(opdaterTid, 15000);
