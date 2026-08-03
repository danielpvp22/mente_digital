// Cliente Android do Mente Digital — Fase 1 (docs/PLANO_APP_ANDROID.md).
//
// As versões abaixo NÃO foram escolhidas por serem "as mais novas": foram
// copiadas de um projeto Android que JÁ COMPILA nesta máquina
// (Desktop/projetos/LibrasCelular), porque o plano registrava "NÃO VERIFIQUEI
// versões específicas" e fixá-las era trabalho desta fase. Toolchain aqui:
// Gradle 9.4.1 (em cache), AGP 9.2.1, Kotlin 2.0.0, JDK 21 (o JBR do Studio),
// SDK em %LOCALAPPDATA%\Android\Sdk com platform android-35.
pluginManagement {
    repositories {
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "MenteDigital"
include(":app")
