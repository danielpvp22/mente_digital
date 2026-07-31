# Roteiro de teste — imagem local × imagem da web (2026-07-31)

50 perguntas, na proporção pedida pelo dono: **5 do acervo do vault (10%)** e
**45 que exigem busca de imagem na web (90%)**.

Regra de leitura do resultado, para não confundir "funcionou" com "apareceu algo":

| Sinal | Significa |
|---|---|
| `![[Figuras/the-cannabis-...]]` | veio do ACERVO (livro do dono) — esperado nos 5 primeiros |
| `![[Figuras/_web/<hash>.webp]]` + linha "(imagem da web — …)" | veio da WEB, já baixada e servida pelo servidor |
| "(Não encontrei imagem disso no acervo do vault nem na web.)" | as três defesas barraram tudo, ou o buscador não devolveu nada |
| nenhuma das três | **defeito** — o pedido de imagem não foi reconhecido |

## Bloco A — do acervo local (5)

1. o que são tricomas? tem imagem?
2. tem foto da deficiência de nitrogênio?
3. me mostra uma imagem de tripes
4. tem figura de como podar a planta?
5. tem imagem de uma lâmpada HPS?

## Bloco B — assunto fora do vault, pedido simples de imagem (25)

6. tem imagem de uma capivara?
7. tem foto da torre Eiffel?
8. me mostra uma imagem de um pinguim-imperador
9. tem figura de um vulcão em erupção?
10. tem imagem da Estátua da Liberdade?
11. me mostra uma foto de uma girafa
12. tem imagem de um satélite Starlink?
13. tem foto de uma aurora boreal?
14. me mostra uma imagem do Monte Fuji
15. tem figura de um polvo?
16. tem imagem de um trem-bala japonês?
17. me mostra uma foto do Coliseu de Roma
18. tem imagem de um camaleão?
19. tem foto de uma tempestade de areia?
20. me mostra uma imagem de um coral cerebral
21. tem imagem de um urso-polar?
22. tem foto de uma placa de vídeo RTX 3080?
23. me mostra uma imagem de um teclado mecânico
24. tem figura de um telescópio James Webb?
25. tem imagem de um dragão-de-komodo?
26. tem foto de uma abelha rainha?
27. me mostra uma imagem de neve em Nova York
28. tem imagem de um farol antigo?
29. tem foto de uma tartaruga-de-couro?
30. me mostra uma imagem de um cacto saguaro

## Bloco C — pedido ESCRITO de web (10)

Aqui o dono manda buscar fora; a web tem de abrir mesmo que o acervo tenha algo.

31. procura na internet uma foto de tricoma
32. busca no google uma imagem de mofo branco
33. pesquisa na web uma foto de folha de cannabis
34. procura na internet uma imagem de estufa de cultivo
35. busca online uma foto de uma lâmpada LED de cultivo
36. procura na internet uma imagem de ácaro-rajado
37. busca no google uma foto de raiz de planta saudável
38. pesquisa na internet uma imagem de tricoma âmbar
39. procura na web uma foto de pulgão
40. busca na internet uma imagem de solo argiloso

## Bloco D — follow-up sem sujeito (10)

O caso do print. Cada par é **duas** mensagens: a pergunta e o follow-up. O
follow-up é que está sendo testado — ele não tem sujeito e depende do turno anterior.

41. `o que é a fotossíntese?` → `tem imagem de um?`
42. `o que é um estômato?` → `tem foto?`
43. `o que é o oídio?` → `tem imagem disso?`
44. `o que é uma bráctea?` → `me mostra uma`
45. `o que é o pistilo da flor?` → `tem figura?`
46. `o que é hidroponia?` → `tem imagem?`
47. `o que é um clone de planta?` → `tem foto de um?`
48. `o que é o pH do solo?` → `tem imagem disso?`
49. `o que é a dominância apical?` → `tem figura de uma?`
50. `o que é um bulbo de sódio de alta pressão?` → `tem foto de um?`

## O que registrar por caso

- a rota (banco/web) e se apareceu imagem, e de qual tipo;
- para o bloco D, se o follow-up entendeu o assunto do turno anterior ou respondeu
  fora do assunto (era o defeito original: "PMC com fita vermelha ou azul");
- qualquer imagem visivelmente **errada** para a pergunta — é o defeito que motivou
  toda a mudança e não pode ter voltado.
