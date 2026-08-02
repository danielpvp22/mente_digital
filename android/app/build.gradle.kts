// ⚠ SEM `kotlin.android` de propósito. O AGP 9 já registra a extensão `kotlin`
// por conta própria, e aplicar o plugin por fora falha com "Cannot add extension
// with name 'kotlin', as there is an extension already registered with that
// name" (medido em 2026-08-02). Só o plugin do compilador Compose entra à parte.
plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.compose.compiler)
}

android {
    namespace = "com.mentedigital.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.mentedigital.app"
        // 24 e não 28: o app é um cliente magro e não usa nada moderno. O que
        // MUDA em 28 é a política de cleartext (ver network_security_config.xml).
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "0.1-fase1"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }
    // Sem `kotlinOptions`: some no AGP 9 junto com o plugin Kotlin avulso (o
    // jvmTarget passa a seguir o `compileOptions` acima).
    buildFeatures { compose = true }
    // `android.util.Log` no android.jar é um esqueleto que levanta "not mocked"
    // em teste de JVM. Aqui ele só registra — devolver 0 não distorce nada, e é
    // o que permite exercitar o ClienteMente sem emulador.
    testOptions { unitTests.isReturnDefaultValues = true }
    packaging { resources { excludes += "/META-INF/{AL2.0,LGPL2.1}" } }
}

dependencies {
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.viewmodel.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.security.crypto)
    implementation(libs.okhttp)

    // Só JUnit puro: o que vale a pena testar nesta fase (parser do protocolo,
    // backoff, montagem de URL) é PURO e roda na JVM, sem emulador. Instrumentado
    // fica para a Fase 2, quando houver áudio para exercitar num aparelho.
    testImplementation(libs.junit)
    testImplementation(libs.json)
}
