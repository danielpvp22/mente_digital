package com.mentedigital.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log

/**
 * Serviço em primeiro plano que mantém a sessão de voz viva.
 *
 * ⚠ Ele NÃO grava nem toca nada — quem faz isso é o `Gravador`/`Reprodutor` do
 * ViewModel. O papel dele é só um: **existir** enquanto a voz está ativa, para o
 * Android não suspender o processo. É a mitigação do Risco R1 do plano, e o
 * plano é explícito de que ela tem três partes e nenhuma sozinha resolve:
 * este serviço, o `pingInterval` do OkHttp (que DETECTA o socket morto, não o
 * impede) e aceitar que sem voz ativa o socket vai cair mesmo — o servidor já
 * reentrega o pendente no próximo accept (ws.py:180-181).
 *
 * Não force uma conexão permanente para resolver um problema que o servidor já
 * resolveu.
 */
class ServicoVoz : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        criarCanal()
        val abrir = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val n: Notification = Notification.Builder(this, CANAL)
            .setContentTitle("Mente Digital está ouvindo")
            .setContentText("Toque para voltar à conversa.")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(abrir)
            .setOngoing(true)
            .build()
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            } else {
                startForeground(ID, n)
            }
        } catch (e: Exception) {
            // Falha aqui (permissão negada, política do fabricante) NÃO pode
            // derrubar o app: a voz continua funcionando com o app em primeiro
            // plano, que é o caso de uso principal. Só perde a sobrevivência em
            // segundo plano — e é melhor perder isso do que travar.
            Log.w(TAG, "não consegui subir em primeiro plano; a voz segue só com o app aberto", e)
            stopSelf()
        }
        return START_NOT_STICKY
    }

    private fun criarCanal() {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        if (nm.getNotificationChannel(CANAL) != null) return
        nm.createNotificationChannel(
            NotificationChannel(CANAL, "Sessão de voz", NotificationManager.IMPORTANCE_LOW)
                .apply { description = "Mantém o microfone ativo enquanto você conversa." }
        )
    }

    companion object {
        private const val TAG = "ServicoVoz"
        private const val CANAL = "voz"
        private const val ID = 1

        fun ligar(ctx: Context) {
            try {
                ctx.startForegroundService(Intent(ctx, ServicoVoz::class.java))
            } catch (e: Exception) {
                Log.w(TAG, "startForegroundService recusado", e)
            }
        }

        fun desligar(ctx: Context) {
            try {
                ctx.stopService(Intent(ctx, ServicoVoz::class.java))
            } catch (e: Exception) {
                Log.w(TAG, "stopService falhou", e)
            }
        }
    }
}
